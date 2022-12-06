"""
Clickhouse backup logic for tables
"""
from collections import deque
from itertools import chain
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ch_backup import logging
from ch_backup.backup.deduplication import (DatabaseDedupInfo, DedupInfo, TableDedupInfo, deduplicate_part)
from ch_backup.backup.layout import BackupLayout
from ch_backup.backup.metadata import BackupMetadata, TableMetadata
from ch_backup.backup.restore_context import RestoreContext
from ch_backup.clickhouse.client import ClickhouseError
from ch_backup.clickhouse.control import ClickhouseCTL
from ch_backup.clickhouse.disks import ClickHouseTemporaryDisks
from ch_backup.clickhouse.models import Table
from ch_backup.clickhouse.schema import (is_atomic_db_engine, is_distributed, is_external_db_engine,
                                         is_materialized_view, is_merge_tree, is_replicated, is_view,
                                         rewrite_table_schema)
from ch_backup.config import Config
from ch_backup.exceptions import ClickhouseBackupError
from ch_backup.logic.backup_manager import BackupManager
from ch_backup.util import compare_schema, get_table_zookeeper_paths
from ch_backup.zookeeper.zookeeper import ZookeeperCTL


class TableBackup(BackupManager):
    """
    Table backup class
    """
    def __init__(self, ch_ctl: ClickhouseCTL, backup_layout: BackupLayout, config: Config) -> None:
        super().__init__(ch_ctl, backup_layout)
        self._config_root = config
        self._config = config['backup']
        self._restore_context = RestoreContext(self._config)
        self._zk_config = config.get('zookeeper')

    def backup(self, backup_meta: BackupMetadata, databases: Sequence[str], db_tables: Dict[str, list],
               dedup_info: DedupInfo, schema_only: bool) -> None:
        """
        Backup tables metadata, MergeTree data and Cloud storage metadata.
        """
        backup_name = backup_meta.get_sanitized_name()

        for db_name in databases:
            self._backup(backup_meta, db_name, db_tables[db_name], backup_name, dedup_info.database(db_name),
                         schema_only)
        self._backup_cloud_storage_metadata(backup_meta)

    def _backup(self, backup_meta: BackupMetadata, db_name: str, tables: Sequence[str], backup_name: str,
                dedup_info: DatabaseDedupInfo, schema_only: bool) -> None:
        """
        Backup single database tables.
        """
        if not self._is_db_external(db_name):
            for table in self._ch_ctl.get_tables(db_name, tables):
                table_meta = TableMetadata(table.database, table.name, table.engine, table.uuid)
                self._backup_table_schema(backup_meta, table)
                if not schema_only:
                    # table_meta will be populated with parts info
                    self._backup_table_data(backup_meta, table, table_meta, backup_name, dedup_info.table(table.name))

                backup_meta.add_table(table_meta)

        self._backup_layout.upload_backup_metadata(backup_meta)

    def _backup_cloud_storage_metadata(self, backup_meta: BackupMetadata) -> None:
        """
        Backup cloud storage metadata files.
        """
        if self._config.get('cloud_storage', {}).get('encryption', True):
            logging.debug('Cloud Storage "shadow" backup will be encrypted')
            backup_meta.cloud_storage.encrypt()

        logging.debug('Backing up Cloud Storage disks "shadow" directory')
        disks = self._ch_ctl.get_disks()
        for disk in disks.values():
            if disk.type == 's3' and not disk.cache_path:
                if not self._backup_layout.upload_cloud_storage_metadata(backup_meta, disk):
                    logging.debug(f'No data frozen on disk "{disk.name}", skipping')
                    continue
                backup_meta.cloud_storage.add_disk(disk.name)
        logging.debug('Cloud Storage disks has been backed up ')

    def restore(self, backup_meta: BackupMetadata, databases: Sequence[str], replica_name: Optional[str],
                cloud_storage_source_bucket: Optional[str], cloud_storage_source_path: Optional[str],
                cloud_storage_latest: bool, clean_zookeeper: bool, schema_only: bool, keep_going: bool) -> None:
        """
        Restore tables and MergeTree data.
        """
        tables_meta: List[TableMetadata] = list(
            chain(*[backup_meta.get_tables(db_name) for db_name in databases if not self._is_db_external(db_name)]))
        tables = list(map(lambda meta: self._get_table_from_meta(backup_meta, meta), tables_meta))

        tables = self._preprocess_tables_to_restore(tables)
        failed_tables = self._restore_tables(tables, clean_zookeeper, replica_name, keep_going)

        # Restore data stored on S3 disks.
        if not schema_only:
            if backup_meta.has_s3_data():
                cloud_storage_source_bucket = cloud_storage_source_bucket if cloud_storage_source_bucket else ''
                cloud_storage_source_path = cloud_storage_source_path if cloud_storage_source_path else ''
                self._restore_cloud_storage_data(backup_meta, cloud_storage_source_bucket, cloud_storage_source_path,
                                                 cloud_storage_latest)

            failed_tables_names = [f"`{t.database}`.`{t.name}`" for t in failed_tables]
            tables_to_restore = filter(lambda t: is_merge_tree(t.engine), tables_meta)
            tables_to_restore = filter(lambda t: f"`{t.database}`.`{t.name}`" not in failed_tables_names,
                                       tables_to_restore)

            with ClickHouseTemporaryDisks(self._ch_ctl, self._backup_layout, self._config_root, backup_meta,
                                          cloud_storage_source_bucket, cloud_storage_source_path) as disks:
                self._restore_data(backup_meta, tables_to_restore, disks)

    def restore_schema(self, source_ch_ctl: ClickhouseCTL, databases: Sequence[str], replica_name: str) -> None:
        """
        Restore schema
        """
        tables: List[Table] = []
        for database in databases:
            tables.extend(source_ch_ctl.get_tables(database))

        tables = self._preprocess_tables_to_restore(tables)
        self._restore_tables(tables, clean_zookeeper=True, replica_name=replica_name)

    def _is_db_external(self, db_name: str) -> bool:
        """
        Return True if DB's engine is one of:
        - MySQL
        - MaterializedMySQL
        - PostgreSQL
        - MaterializedPostgreSQL
        or False otherwise
        """
        return is_external_db_engine(self._ch_ctl.get_database_engine(db_name))

    def _backup_table_schema(self, backup_meta: BackupMetadata, table: Table) -> None:
        """
        Backup table object.
        """
        logging.debug('Uploading table schema for "%s"."%s"', table.database, table.name)

        self._backup_layout.upload_table_create_statement(backup_meta.name, table.database, table.name,
                                                          table.create_statement)

    def _backup_table_data(self, backup_meta: BackupMetadata, table: Table, table_meta: TableMetadata,
                           backup_name: str, dedup_info: TableDedupInfo) -> None:
        """
        Backup table with data opposed to schema only.
        """
        logging.debug('Performing table backup for "%s"."%s"', table.database, table.name)

        if not is_merge_tree(table.engine):
            logging.info('Skipping table backup for non MergeTree table "%s"."%s"', table.database, table.name)
            return

        try:
            self._ch_ctl.freeze_table(backup_name, table)
        except ClickhouseError:
            if self._ch_ctl.does_table_exist(table.database, table.name):
                raise

            logging.warning('Table "%s"."%s" was removed by a user during backup', table.database, table.name)
            return

        uploaded_parts = []
        for data_path, disk in table.paths_with_disks:
            freezed_parts = self._ch_ctl.list_freezed_parts(table, disk, data_path, backup_name)

            for fpart in freezed_parts:
                logging.debug('Working on %s', fpart)

                if disk.type == 's3':
                    table_meta.add_part(fpart.to_part_metadata())
                    continue

                # trying to find part in storage
                part = deduplicate_part(self._backup_layout, fpart, dedup_info)
                if part:
                    self._ch_ctl.remove_freezed_part(fpart)
                else:
                    self._backup_layout.upload_data_part(backup_meta.name, fpart)
                    part = fpart.to_part_metadata()
                    uploaded_parts.append(part)

                table_meta.add_part(part)  # type: ignore

        self._backup_layout.wait()

        self._validate_uploaded_parts(backup_meta, uploaded_parts)

        self._ch_ctl.remove_freezed_data()

    def _validate_uploaded_parts(self, backup_meta, uploaded_parts):
        if self._config['validate_part_after_upload']:
            invalid_parts = []

            for part in uploaded_parts:
                if not self._backup_layout.check_data_part(backup_meta.path, part):
                    invalid_parts.append(part)

            if invalid_parts:
                for part in invalid_parts:
                    logging.error(f'Uploaded part is broken, {part.database}.{part.table}: {part.name}')
                raise RuntimeError(f'Uploaded parts are broken, {", ".join(map(lambda p: p.name, invalid_parts))}')

    def _preprocess_tables_to_restore(self, tables: List[Table]) -> List[Table]:
        # Prepare table schema to restore.
        for table in tables:
            self._rewrite_table_schema(table)

        # Filter out already restored tables.
        existing_tables = {}
        for table in self._ch_ctl.get_tables():
            existing_tables[(table.database, table.name)] = table

        result: List[Table] = []
        for table in tables:
            existing_table = existing_tables.get((table.database, table.name))
            if existing_table:
                if compare_schema(existing_table.create_statement, table.create_statement):
                    continue
                logging.warning(
                    'Table "%s"."%s" will be recreated as its schema mismatches the schema from backup: "%s" != "%s"',
                    table.database, table.name, existing_table.create_statement, table.create_statement)
                self._ch_ctl.drop_table_if_exists(table.database, table.name)

            result.append(table)

        return result

    def _get_table_from_meta(self, backup_meta: BackupMetadata, meta: TableMetadata) -> Table:
        return Table(
            database=meta.database,
            name=meta.name,
            engine=meta.engine,
            # TODO: set disks and data_paths
            disks=[],
            data_paths=[],
            uuid=meta.uuid,
            create_statement=self._backup_layout.get_table_create_statement(backup_meta, meta.database, meta.name))

    def _restore_tables(self,
                        tables: Iterable[Table],
                        clean_zookeeper: bool = False,
                        replica_name: Optional[str] = None,
                        keep_going: bool = False) -> List[Table]:
        merge_tree_tables = []
        distributed_tables = []
        view_tables = []
        other_tables = []
        for table in tables:
            self._rewrite_table_schema(table, add_uuid_if_required=True)

            if is_merge_tree(table.engine):
                merge_tree_tables.append(table)
            elif is_distributed(table.engine):
                distributed_tables.append(table)
            elif is_view(table.engine):
                view_tables.append(table)
            else:
                other_tables.append(table)

        if clean_zookeeper and len(self._zk_config.get('hosts')) > 0:
            macros = self._ch_ctl.get_macros()
            replicated_tables = [table for table in merge_tree_tables if is_replicated(table.engine)]
            zk_ctl = ZookeeperCTL(self._zk_config)
            zk_ctl.delete_replica_metadata(get_table_zookeeper_paths(replicated_tables), replica_name, macros)

        return self._restore_table_objects(chain(merge_tree_tables, other_tables, distributed_tables, view_tables),
                                           keep_going)

    def _restore_cloud_storage_data(self, backup_meta: BackupMetadata, source_bucket: str, source_path: str,
                                    cloud_storage_latest: bool) -> None:
        for disk_name, revision in backup_meta.s3_revisions.items():
            logging.debug(f'Restore disk {disk_name} to revision {revision}')

            self._ch_ctl.create_s3_disk_restore_file(disk_name, revision if not cloud_storage_latest else 0,
                                                     source_bucket, source_path)

            if self._restore_context.disk_restarted(disk_name):
                logging.debug(f'Skip restoring disk {disk_name} as it has already been restored')
                continue

            try:
                self._ch_ctl.restart_disk(disk_name, self._restore_context)
            finally:
                self._restore_context.dump_state()

    def _restore_data(self, backup_meta: BackupMetadata, tables: Iterable[TableMetadata],
                      disks: ClickHouseTemporaryDisks) -> None:
        for table_meta in tables:
            try:
                logging.debug('Running table "%s.%s" data restore', table_meta.database, table_meta.name)

                self._restore_context.add_table(table_meta)
                maybe_table = self._ch_ctl.get_table(table_meta.database, table_meta.name)
                assert maybe_table is not None, f'Table not found {table_meta.database}.{table_meta.name}'
                table: Table = maybe_table

                attach_parts = []
                for part in table_meta.get_parts():
                    if self._restore_context.part_restored(part):
                        logging.debug(f'{table.database}.{table.name} part {part.name} already restored, skipping it')
                        continue

                    if part.disk_name not in backup_meta.s3_revisions.keys():
                        if part.disk_name in backup_meta.cloud_storage.disks:
                            disks.copy_part(backup_meta, table, part)
                        else:
                            fs_part_path = self._ch_ctl.get_detached_part_path(table, part.disk_name, part.name)
                            self._backup_layout.download_data_part(backup_meta, part, fs_part_path)

                    attach_parts.append(part)

                self._backup_layout.wait()

                self._ch_ctl.chown_detached_table_parts(table, self._restore_context)
                for part in attach_parts:
                    try:
                        logging.debug('Attaching "%s.%s" part: %s', table_meta.database, table.name, part.name)
                        self._ch_ctl.attach_part(table, part.name)
                        self._restore_context.add_part(part)
                    except Exception as e:
                        logging.warning('Attaching "%s.%s" part %s failed: %s', table_meta.database, table.name,
                                        part.name, repr(e))
                        self._restore_context.add_failed_part(part, e)
            finally:
                self._restore_context.dump_state()

    def _rewrite_table_schema(self, table: Table, add_uuid_if_required: bool = False) -> None:
        add_uuid = False
        inner_uuid = None
        if add_uuid_if_required and table.uuid and self._is_db_atomic(table.database):
            add_uuid = True
            # Starting with 21.4 it's required to explicitly set inner table UUID for materialized views.
            if is_materialized_view(table.engine) and self._ch_ctl.ch_version_ge('21.4'):
                inner_table = self._ch_ctl.get_table(table.database, f'.inner_id.{table.uuid}')
                if inner_table:
                    inner_uuid = inner_table.uuid

        rewrite_table_schema(table,
                             force_non_replicated_engine=self._config['force_non_replicated'],
                             override_replica_name=self._config['override_replica_name'],
                             add_uuid=add_uuid,
                             inner_uuid=inner_uuid)

    def _restore_table_objects(self, tables: Iterable[Table], keep_going: bool = False) -> List[Table]:
        errors: List[Tuple[Union[Table], Exception]] = []
        unprocessed = deque(table for table in tables)
        while unprocessed:
            table = unprocessed.popleft()
            try:
                self._ch_ctl.restore_table(table.database, table.name, table.engine, table.create_statement)
            except Exception as e:
                errors.append((table, e))
                unprocessed.append(table)
                if len(errors) > len(unprocessed):
                    break
                logging.warning(f'Failed to restore "{table.database}"."{table.name}" with "{repr(e)}",'
                                ' will retry after restoring other tables')
            else:
                errors.clear()

        if errors:
            logging.error('Failed to restore tables:\n%s',
                          '\n'.join(f'"{v.database}"."{v.name}": {e!r}' for v, e in errors))

            if keep_going:
                return list(set(table for table, _ in errors))

            failed_tables = sorted(list(set(f'`{t.database}`.`{t.name}`' for t in set(table for table, _ in errors))))
            raise ClickhouseBackupError(f'Failed to restore tables: {", ".join(failed_tables)}')

        return []

    def _is_db_atomic(self, db_name: str) -> bool:
        """
        Return True if database engine is Atomic, or False otherwise.
        """
        return is_atomic_db_engine(self._ch_ctl.get_database_engine(db_name))
