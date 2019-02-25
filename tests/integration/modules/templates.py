"""
Module responsible for template rendering.
"""
import os

from jinja2 import BaseLoader, Environment, FileSystemLoader, StrictUndefined

from . import docker
from .datetime import decrease_time_str, increase_time_str
from .typing import ContextT
from .utils import context_to_dict, env_stage

TEMP_FILE_EXT = 'temp~'


@env_stage('create', fail=True)
def render_configs(context: ContextT) -> None:
    """
    Render each template in the subtree.
    Each template is rendered in-place. As the framework operates in
    staging dir, this is easily reset by `make clean`, or `rm -fr staging`.
    """
    staging_dir = context.conf['staging_dir']
    for project in context.conf['projects']:
        project_dir = '{0}/images/{1}'.format(staging_dir, project)
        for root, _, files in os.walk(project_dir):
            for basename in files:
                if not basename.endswith(TEMP_FILE_EXT):
                    _render_file(context, root, basename)


def render_template(context: ContextT, text: str) -> str:
    """
    Render template passed as a string.
    """
    template = _environment(context).from_string(text)
    return template.render(context_to_dict(context))


def _render_file(context: ContextT, directory: str, basename: str) -> None:
    path = '%s/%s' % (directory, basename)
    temp_file_path = '%s.%s' % (path, TEMP_FILE_EXT)
    loader = FileSystemLoader(directory)
    environment = _environment(context, loader)
    jinja_context = context_to_dict(context)
    try:
        with open(temp_file_path, 'w') as temp_file:
            template = environment.get_template(basename)
            temp_file.write(template.render(jinja_context))
    except Exception as e:
        raise RuntimeError('Failed to render {0}'.format(path)) from e
    os.rename(temp_file_path, path)


def _environment(context: ContextT, loader: BaseLoader = None) -> Environment:
    """
    Create Environment object.
    """

    def _get_file_size(container_name, path):
        container = docker.get_container(context, container_name)
        return docker.get_file_size(container, path)

    environment = Environment(
        autoescape=False,
        trim_blocks=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        loader=loader)

    environment.filters['increase_on'] = increase_time_str
    environment.filters['decrease_on'] = decrease_time_str

    environment.globals['get_file_size'] = _get_file_size

    return environment
