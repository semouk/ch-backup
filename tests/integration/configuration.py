"""
Variables that influence testing behavior are defined here.
"""

import os
import random

from tests.integration.helpers import crypto
from tests.integration.helpers.utils import merge

try:
    from local_configuration import CONF_OVERRIDE
except ImportError:
    CONF_OVERRIDE = {}


def get():
    """
    Get configuration (non-idempotent function)
    """
    # This "factor" is later used in the network name and port forwarding.
    port_factor = random.randint(0, 4096)
    # Docker network name. Also used as a project and domain name.
    net_name = 'test_net_{num}'.format(num=port_factor)

    dynamic_config = generate_dynamic_config(net_name)

    config = {
        # Common conf options.
        # See below for dynamic stuff (keys, certs, etc)
        'dynamic':
            dynamic_config,
        # Controls whether to perform cleanup after tests execution or not.
        'cleanup':
            True,
        # Code checkout
        # Where does all the fun happens.
        # Assumption is that it can be safely rm-rf`ed later.
        'staging_dir':
            'staging',
        # Default repository to pull code from.
        'git_repo_base':
            os.environ.get('DBAAS_INFRA_REPO_BASE',
                           'ssh://{user}@gerrit.cmail.yandex.net:9501'),
        # Controls whether overwrite existing locally checked out
        # code or not (default)
        'git_clone_overwrite':
            False,
        # If present, git.checkout_code will attempt to checkout this topic.
        'gerrit_topic':
            os.environ.get('GERRIT_TOPIC'),
        # Docker-related
        'docker_ip4_subnet':
            '10.%s.0/24',
        'docker_ip6_subnet':
            'fd00:dead:beef:%s::/96',
        # See above.
        'port_factor':
            port_factor,
        # These docker images are brewed on `docker.prep_images` stage.
        # Options below are passed as-is to
        # <docker_api_instance>.container.create()
        'base_images': {
            'dbaas-ch-backup-base': {
                'tag': 'dbaas-ch-backup-base',
                'path': 'images/base',
            },
        },
        # Docker network name. Also doubles as a project and domain name.
        'network_name':
            net_name,

        # A dict with all projects that are going to interact in this
        # testing environment.
        'projects': {
            # Basically this mimics docker-compose 'service'.
            # Matching keys will be used in docker-compose,
            # while others will be ignored in compose file, but may be
            # referenced in any other place.
            'base': {
                # The base needs to be present so templates,
                # if any, will be rendered.
                # It is brewed by docker directly,
                # and not used in compose environment.
                'docker_instances': 0,
            },
            'clickhouse': {
                'build':
                    '..',
                # Config can have arbitrary keys.
                # This one is used in template matching of config file options.
                # See Dockerfile itself for examples.
                # 'docker_instances': 3,
                'db': {
                    'user': 'dbaas_reader',
                    'password': 'dbaas_reader_password',
                },
                'expose': {
                    'http': 8123,
                    'clickhouse': 9000,
                },
                'docker_instances':
                    2,
                'external_links': [
                    '%s:%s' % (dynamic_config['s3']['host'],
                               dynamic_config['s3']['fake_host']),
                ],
            },
            'minio': {
                'build': 'images/minio',
                'expose': {
                    'http': 9000,
                },
            },
        },
    }
    return merge(config, CONF_OVERRIDE)


def generate_dynamic_config(net_name):
    """
    Generates dynamic stuff like keys, uuids and other.
    """
    keys = {
        'internal_api': crypto.gen_keypair(),
        'client': crypto.gen_keypair(),
    }
    # https://pynacl.readthedocs.io/en/latest/public/#nacl-public-box
    # CryptoBox is a subclass of Box, but returning a string instead.
    api_to_client_box = crypto.CryptoBox(
        keys['internal_api']['secret_obj'],
        keys['client']['public_obj'],
    )
    s3_credentials = {
        'access_secret_key': crypto.gen_plain_random_string(40),
        'access_key_id': crypto.gen_plain_random_string(20),
    }
    config = {
        's3': {
            'host':
                'minio01.{domain}'.format(domain=net_name),
            'fake_host':
                'minio',
            'bucket':
                'dbaas',
            'endpoint':
                'http://minio:9000',
            'access_secret_key':
                s3_credentials['access_secret_key'],
            'access_key_id':
                s3_credentials['access_key_id'],
            'enc_access_secret_key':
                api_to_client_box.encrypt_utf(
                    s3_credentials['access_secret_key']),
            'enc_access_key_id':
                api_to_client_box.encrypt_utf(s3_credentials['access_key_id']),
        },
        'ch_backup': {
            'encrypt_key': crypto.gen_plain_random_string(32),
        },
    }

    return config
