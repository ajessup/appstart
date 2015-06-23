# Copyright 2015 Google Inc. All Rights Reserved.
"""Helper functions for components of appstart."""
# This file follows the external style guide.
# pylint: disable=bad-indentation, g-bad-import-order

import logging
import io
import json
import os
import requests
import socket
import ssl
import sys
import tarfile
import tempfile

import docker


# HTTP timeout for docker client
TIMEOUT_SECS = 60

# Default docker host if user isn't using boot2docker
LINUX_DOCKER_HOST = '/var/run/docker.sock'

# Logger that is shared accross all components of appstart
_logger = None

# Logging format
FMT = '[%(levelname).1s: %(asctime)s] %(message)s'
DATE_FMT = '%H:%M:%S'


def get_logger():
    """Configures the appstart logger if it doesn't exist yet.

    Returns:
        (logging.Logger) a logger used to log messages on behalf of
        appstart.
    """
    global _logger
    if _logger is None:
        _logger = logging.getLogger('appstart')
        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)
        formatter = logging.Formatter(fmt=FMT, datefmt=DATE_FMT)
        sh.setFormatter(formatter)
        _logger.addHandler(sh)
    return _logger


def get_docker_client():
    """Get the user's docker client.

    Returns:
        (docker.Client) a docker client that can be used to manage
        containers and images.
    """
    host = os.environ.get('DOCKER_HOST')
    cert_path = os.environ.get('DOCKER_CERT_PATH')
    tls_verify = int(os.environ.get('DOCKER_TLS_VERIFY', 0))

    params = {}
    if host:
        params['base_url'] = (host.replace('tcp://', 'https://')
                              if tls_verify else host)
    elif sys.platform.startswith('linux'):
        # if this is a linux user, the default value of DOCKER_HOST
        # should be the unix socket.  first check if the socket is
        # valid to give a better feedback to the user.
        if os.path.exists(LINUX_DOCKER_HOST):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(LINUX_DOCKER_HOST)
                params['base_url'] = 'unix://' + LINUX_DOCKER_HOST
            except socket.error:
                get_logger().warning('Found a stale '
                                     '/var/run/docker.sock, '
                                     'did you forget to start '
                                     'your docker daemon?')
            finally:
                sock.close()

    if tls_verify and cert_path:
        # assert_hostname=False is needed for boot2docker to work with
        # our custom registry.
        params['tls'] = docker.tls.TLSConfig(
            client_cert=(os.path.join(cert_path, 'cert.pem'),
                         os.path.join(cert_path, 'key.pem')),
            ca_cert=os.path.join(cert_path, 'ca.pem'),
            verify=True,
            ssl_version=ssl.PROTOCOL_TLSv1,
            assert_hostname=False)

    # pylint: disable=star-args
    client = docker.Client(version='1.17',
                           timeout=TIMEOUT_SECS,
                           **params)
    try:
        client.ping()
    except requests.exceptions.ConnectionError as excep:
        get_logger().error('Failed to connect to Docker '
                           'Daemon due to: %s', excep)
        raise
    return client


def make_tar_build_context(dockerfile, context_files):
        """Compose tar file for the new devappserver layer's build context.

        Args:
            dockerfile: (io.BytesIO or file) a file-like object
                representing the Dockerfile.
            context_files: ({basestring: basestring, ...}) a dictionary
                mapping absolute filepaths to their destination name in
                the tar build context. This is used to specify other files
                that should be added to the build context.

        Returns:
            (tempfile.NamedTemporaryFile) a temporary tarfile
            representing the docker build context.
        """
        f = tempfile.NamedTemporaryFile()
        t = tarfile.open(mode='w', fileobj=f)

        # Add dockerfile to top level under the name "Dockerfile"
        if isinstance(dockerfile, io.BytesIO):
            dfinfo = tarfile.TarInfo('Dockerfile')
            dfinfo.size = len(dockerfile.getvalue())
            dockerfile.seek(0)
        else:
            dfinfo = t.gettarinfo(fileobj=dockerfile, arcname='Dockerfile')
        t.addfile(dfinfo, dockerfile)

        # Open all of the context files and add them to the tarfile.
        for path in context_files:
            with open(path) as file_object:
                file_info = t.gettarinfo(fileobj=file_object,
                                         arcname=context_files[path])
                t.addfile(file_info, file_object)

        t.close()
        f.seek(0)
        return f


def log_and_check_build_results(build_res, image_name):
        """Log the results of a docker build.

        Args:
            build_res: ([basestring, ...]) a generator of build results,
                as returned by docker.Client.build
            image_name: (basestring) the name of the image associated
                with the build results (for logging purposes only)

        Raises:
            docker.errors.DockerException: if the build failed.
        """
        get_logger().info('-' * 20 + '  BUILDING IMAGE  ' + '-' * 20)
        get_logger().info('IMAGE  : %s', image_name)

        success = True
        try:
            for chunk in build_res:
                if not chunk:
                    continue
                line = json.loads(chunk)
                if 'stream' in line:
                    logmsg = line['stream'].strip()
                    get_logger().info(logmsg)
                elif 'error' in line:
                    success = False
                    logmsg = line['error'].strip()
                    get_logger().error(logmsg)
                elif 'errorDetail' in line:
                    success = False
                    logmsg = line['errorDetail']['message'].strip()
                    get_logger().error(logmsg)
        finally:
            get_logger().info('-' * 58)

        if not success:
            raise docker.errors.DockerException('Image build failed.')