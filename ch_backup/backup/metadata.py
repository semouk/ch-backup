"""
Backup metadata.
"""
import json
import socket
from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Set

from ch_backup.exceptions import InvalidBackupStruct, UnknownBackupStateError
from ch_backup.util import now


class BackupState(Enum):
    """
    Backup states.
    """

    CREATED = 'created'
    CREATING = 'creating'
    DELETING = 'deleting'
    PARTIALLY_DELETED = 'partially_deleted'
    FAILED = 'failed'


class PartMetadata(SimpleNamespace):
    """
    Backup metadata for ClickHouse data part.
    """

    # pylint: disable=too-many-arguments
    def __init__(self,
                 database: str,
                 table: str,
                 name: str,
                 checksum: str,
                 size: int,
                 files: Sequence[str],
                 tarball: bool,
                 link: str = None,
                 disk_name: str = None) -> None:
        super().__init__()
        self.database: str = database
        self.table: str = table
        self.name: str = name
        self.raw_metadata: dict = {
            'checksum': checksum,
            'size': size,
            'files': files,
            'tarball': tarball,
            'link': link,
            'disk_name': disk_name,
        }

    @property
    def checksum(self) -> str:
        """
        Return data part checksum.
        """
        return self.raw_metadata['checksum']

    @property
    def size(self) -> int:
        """
        Return data part size.
        """
        return self.raw_metadata['size']

    @property
    def files(self) -> Sequence[str]:
        """
        Return data part files.
        """
        return self.raw_metadata['files']

    @property
    def link(self) -> Optional[str]:
        """
        For deduplicated data parts it returns link to the source backup (its path). Otherwise None is returned.
        """
        return self.raw_metadata['link']

    @property
    def disk_name(self) -> str:
        """
        Return disk name where part is stored.
        """
        return self.raw_metadata.get('disk_name', 'default')

    @property
    def tarball(self) -> bool:
        """
        Returns true if part files stored as single tarball.
        """
        return self.raw_metadata['tarball']

    @classmethod
    def load(cls, db_name: str, table_name: str, part_name: str, raw_metadata: dict) -> 'PartMetadata':
        """
        Deserialize data part metadata.
        """
        return cls(database=db_name,
                   table=table_name,
                   name=part_name,
                   checksum=raw_metadata['checksum'],
                   size=raw_metadata['bytes'],
                   files=raw_metadata['files'],
                   tarball=raw_metadata.get('tarball', False),
                   link=raw_metadata['link'],
                   disk_name=raw_metadata.get('disk_name', 'default'))


class TableMetadata(SimpleNamespace):
    """
    Backup metadata for ClickHouse table.
    """
    def __init__(self, database: str, name: str, engine: str, uuid: Optional[str]) -> None:
        super().__init__()
        self.database: str = database
        self.name: str = name
        self.raw_metadata: dict = {
            'engine': engine,
            'uuid': uuid,
            'parts': {},
        }

    @property
    def engine(self) -> str:
        """
        Return table engine.
        """
        return self.raw_metadata['engine']

    @property
    def uuid(self) -> Optional[str]:
        """
        Return uuid of the table if not zero. Used for view restore in ch > 20.10
        """
        return self.raw_metadata['uuid']

    def get_parts(self, *, excluded_parts: Set[str] = None) -> List[PartMetadata]:
        """
        Return data parts.
        """
        if not excluded_parts:
            excluded_parts = set()

        result = []
        for part_name, raw_metadata in self.raw_metadata['parts'].items():
            if part_name not in excluded_parts:
                result.append(PartMetadata.load(self.database, self.name, part_name, raw_metadata))

        return result

    def add_part(self, part: PartMetadata) -> None:
        """
        Add data part to metadata.
        """
        assert part.database == self.database
        assert part.table == self.name
        assert part.name not in self.raw_metadata['parts']

        self.raw_metadata['parts'][part.name] = {
            'checksum': part.checksum,
            'bytes': part.size,
            'files': part.files,
            'link': part.link,
            'tarball': part.tarball,
            'disk_name': part.disk_name,
        }

    @classmethod
    def load(cls, database: str, name: str, raw_metadata: dict) -> 'TableMetadata':
        """
        Deserialize table metadata.
        """
        table = cls(database, name, raw_metadata['engine'], raw_metadata.get('uuid', None))
        table.raw_metadata['parts'] = raw_metadata['parts']
        return table


class BackupMetadata:
    """
    Backup metadata.
    """

    # pylint: disable=too-many-instance-attributes
    # pylint: disable=too-many-arguments

    def __init__(self,
                 name: str,
                 path: str,
                 version: str,
                 ch_version: str,
                 time_format: str,
                 hostname: str = None,
                 labels: dict = None,
                 schema_only: bool = False) -> None:
        self.name = name
        self.labels = labels
        self.path = path
        self.version = version
        self.ch_version = ch_version
        self.hostname = hostname or socket.getfqdn()
        self._state = BackupState.CREATING
        self.time_format = time_format
        self.start_time = now()
        self.end_time: Optional[datetime] = None
        self._databases: Dict[str, dict] = {}
        self._access_control: List[str] = []
        self.size = 0
        self.real_size = 0
        self.schema_only = schema_only
        self.s3_revisions: Dict[str, int] = {}  # S3 disk name -> revision counter.

    def __str__(self) -> str:
        return self.dump_json()

    @property
    def state(self) -> BackupState:
        """
        Backup state.
        """
        return self._state

    @state.setter
    def state(self, value: BackupState) -> None:
        if value not in BackupState:
            raise UnknownBackupStateError
        self._state = value

    @property
    def start_time_str(self) -> str:
        """
        String representation of backup start time.
        """
        return self._format_time(self.start_time)

    @property
    def end_time_str(self) -> Optional[str]:
        """
        String representation of backup end time.
        """
        return self._format_time(self.end_time) if self.end_time else None

    def update_end_time(self) -> None:
        """
        Set end time to the current time.
        """
        self.end_time = now()

    def dump_json(self, light: bool = False) -> str:
        """
        Return json representation of backup metadata.
        """
        report = {
            'databases': self._databases if not light else {},
            'access_control': self._access_control if not light else [],
            'meta': {
                'name': self.name,
                'path': self.path,
                'version': self.version,
                'ch_version': self.ch_version,
                'hostname': self.hostname,
                'time_format': self.time_format,
                'start_time': self.start_time_str,
                'end_time': self.end_time_str,
                'bytes': self.size,
                'real_bytes': self.real_size,
                'state': self._state.value,
                'labels': self.labels,
                # TODO: clean up backward-compatibility logic (delete 'date_fmt'); it's required changes in int api
                # to replace 'date_fmt' with 'time_format'.
                'date_fmt': self.time_format,
                'schema_only': self.schema_only,
                's3_revisions': self.s3_revisions,
            },
        }
        return json.dumps(report, separators=(',', ':'))

    @classmethod
    def load(cls, data: dict) -> 'BackupMetadata':
        """
        Deserialize backup metadata.
        """
        # pylint: disable=protected-access
        try:
            meta = data['meta']

            backup = cls.__new__(cls)
            backup.name = meta['name']
            backup.path = meta['path']
            backup.hostname = meta['hostname']
            backup.time_format = meta['time_format']
            backup._databases = data['databases']
            backup._access_control = data.get('access_control', [])
            backup.start_time = cls._load_time(meta, 'start_time')
            backup.end_time = cls._load_time(meta, 'end_time')
            backup.size = meta['bytes']
            backup.real_size = meta['real_bytes']
            backup._state = BackupState(meta['state'])
            backup.ch_version = meta['ch_version']
            backup.labels = meta['labels']
            backup.version = meta['version']
            backup.schema_only = meta.get('schema_only', False)
            backup.s3_revisions = meta.get('s3_revisions', {})

            return backup

        except (ValueError, KeyError):
            raise InvalidBackupStruct

    @classmethod
    def load_json(cls, data):
        """
        Deserialize backup metadata from JSON representation.
        """
        return cls.load(json.loads(data))

    def get_databases(self) -> Sequence[str]:
        """
        Get databases.
        """
        return tuple(self._databases.keys())

    def add_database(self, db_name: str) -> None:
        """
        Add database to backup metadata.
        """
        assert db_name not in self._databases

        self._databases[db_name] = {
            'tables': {},
        }

    def get_tables(self, db_name: str) -> Sequence[TableMetadata]:
        """
        Get tables for the specified database.
        """
        result = []
        for table_name, raw_metadata in self._databases[db_name]['tables'].items():
            result.append(TableMetadata.load(db_name, table_name, raw_metadata))

        return result

    def get_table(self, db_name: str, table_name: str) -> TableMetadata:
        """
        Get the specified table.
        """
        return TableMetadata.load(db_name, table_name, self._databases[db_name]['tables'][table_name])

    def add_table(self, table: TableMetadata) -> None:
        """
        Add table to backup metadata.
        """
        tables = self._databases[table.database]['tables']

        assert table.name not in tables

        tables[table.name] = table.raw_metadata

        for part in table.get_parts():
            self.size += part.size
            if not part.link:
                self.real_size += part.size

    def get_parts(self) -> Sequence[PartMetadata]:
        """
        Get data parts of all tables.
        """
        parts: List[PartMetadata] = []
        for db_name in self.get_databases():
            for table in self.get_tables(db_name):
                parts.extend(table.get_parts())

        return parts

    def find_part(self, db_name: str, table_name: str, part_name: str) -> Optional[PartMetadata]:
        """
        Find and return data part. If not found, None is returned.
        """
        try:
            part = self._databases[db_name]['tables'][table_name]['parts'][part_name]
            return PartMetadata.load(db_name, table_name, part_name, part)
        except KeyError:
            return None

    def add_part(self, part: PartMetadata) -> None:
        """
        Add data part to backup metadata.
        """
        self.get_table(part.database, part.table).add_part(part)

        self.size += part.size
        if not part.link:
            self.real_size += part.size

    def remove_parts(self, table: TableMetadata, parts: List[PartMetadata]) -> None:
        """
        Remove data parts from backup metadata.
        """
        _parts = self._databases[table.database]['tables'][table.name]['parts']

        for part in parts:
            del _parts[part.name]

            self.size -= part.size
            if not part.link:
                self.real_size -= part.size

    def is_empty(self) -> bool:
        """
        Return True if backup has no data.
        """
        return self.size == 0

    def get_access_control(self) -> Sequence[str]:
        """
        Get access control objects.
        """
        return self._access_control

    def set_access_control(self, objects: List[str]) -> None:
        """
        Add access control objects to backup metadata.
        """
        self._access_control = objects

    def has_s3_data(self) -> bool:
        """
        Return True if backup has data on S3 disks.
        """
        return len(self.s3_revisions) > 0

    def _format_time(self, value: datetime) -> str:
        return value.strftime(self.time_format)

    @staticmethod
    def _load_time(meta: dict, attr: str) -> Optional[datetime]:
        attr_value = meta.get(attr)
        if not attr_value:
            return None

        result = datetime.strptime(attr_value, meta['time_format'])
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)

        return result
