# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright (C) 2015, 2016 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import fileinput
import glob
import itertools
import logging
import os
import platform
import re
import shutil
import stat
import string
import subprocess
import urllib
import urllib.request

from xml.etree import ElementTree

import snapcraft
from snapcraft.internal import common

import re
import platform

dist = platform.linux_distribution()[0]

if re.match('Ubuntu',dist):
    from snapcraft.internal.repo.ubuntu import Repo
    from snapcraft.internal.repo.ubuntu import install_build_packages
    import apt as dRepo
elif re.match('.*SUSE.*',dist):
    from snapcraft.internal.repo.suse import Repo
    from snapcraft.internal.repo.suse import install_build_packages
    from snapcraft.internal import zypper as dRepo
else:
    raise NotImplementedError


_BIN_PATHS = (
    'bin',
    'sbin',
    'usr/bin',
    'usr/sbin',
)

logger = logging.getLogger(__name__)

def is_package_installed(package):
    """Return True if a package is installed on the system.

    :param str package: the deb package to query for.
    :returns: True if the package is installed, False if not.
    """
    with dRepo.Cache() as repo_cache:
        return repo_cache[package].installed()


class PackageNotFoundError(Exception):

    @property
    def message(self):
        return 'The Ubuntu package "{}" was not found'.format(
            self.package_name)

    def __init__(self, package_name):
        self.package_name = package_name


class UnpackError(Exception):

    @property
    def message(self):
        return 'Error while provisioning "{}"'.format(self.package_name)

    def __init__(self, package_name):
        self.package_name = package_name



def fix_pkg_config(root, pkg_config_file, prefix_trim=None):
    """Opens a pkg_config_file and prefixes the prefix with root."""
    pattern_trim = None
    if prefix_trim:
        pattern_trim = re.compile(
            '^prefix={}(?P<prefix>.*)'.format(prefix_trim))
    pattern = re.compile('^prefix=(?P<prefix>.*)')

    with fileinput.input(pkg_config_file, inplace=True) as input_file:
        for line in input_file:
            match = pattern.search(line)
            if prefix_trim:
                match_trim = pattern_trim.search(line)
            if prefix_trim and match_trim:
                print('prefix={}{}'.format(root, match_trim.group('prefix')))
            elif match:
                print('prefix={}{}'.format(root, match.group('prefix')))
            else:
                print(line, end='')


def _fix_artifacts(debdir):
    '''
    Sometimes debs will contain absolute symlinks (e.g. if the relative
    path would go all the way to root, they just do absolute).  We can't
    have that, so instead clean those absolute symlinks.

    Some unpacked items will also contain suid binaries which we do not want in
    the resulting snap.
    '''
    for root, dirs, files in os.walk(debdir):
        # Symlinks to directories will be in dirs, while symlinks to
        # non-directories will be in files.
        for entry in itertools.chain(files, dirs):
            path = os.path.join(root, entry)
            if os.path.islink(path) and os.path.isabs(os.readlink(path)):
                _fix_symlink(path, debdir, root)
            elif os.path.exists(path):
                _fix_filemode(path)

            if path.endswith('.pc') and not os.path.islink(path):
                fix_pkg_config(debdir, path)


def _fix_xml_tools(root):
    xml2_config_path = os.path.join(root, 'usr', 'bin', 'xml2-config')
    if os.path.isfile(xml2_config_path):
        common.run(
            ['sed', '-i', '-e', 's|prefix=/usr|prefix={}/usr|'.
                format(root), xml2_config_path])

    xslt_config_path = os.path.join(root, 'usr', 'bin', 'xslt-config')
    if os.path.isfile(xslt_config_path):
        common.run(
            ['sed', '-i', '-e', 's|prefix=/usr|prefix={}/usr|'.
                format(root), xslt_config_path])


def _fix_symlink(path, debdir, root):
    target = os.path.join(debdir, os.readlink(path)[1:])
    if _skip_link(os.readlink(path)):
        logger.debug('Skipping {}'.format(target))
        return
    if not os.path.exists(target) and not _try_copy_local(path, target):
        return
    os.remove(path)
    os.symlink(os.path.relpath(target, root), path)


def _fix_filemode(path):
    mode = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    if mode & 0o4000 or mode & 0o2000:
        logger.warning('Removing suid/guid from {}'.format(path))
        os.chmod(path, mode & 0o1777)


def _fix_shebangs(path):
    """Changes hard coded shebangs for files in _BIN_PATHS to use env."""
    paths = [p for p in _BIN_PATHS if os.path.exists(os.path.join(path, p))]
    for p in [os.path.join(path, p) for p in paths]:
        common.replace_in_file(p, re.compile(r''),
                               re.compile(r'#!.*python\n'),
                               r'#!/usr/bin/env python\n')


def _try_copy_local(path, target):
    real_path = os.path.realpath(path)
    if os.path.exists(real_path):
        logger.warning(
            'Copying needed target link from the system {}'.format(real_path))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        link_src = os.readlink(path)

        if os.path.is_directory(link_src):
            logger.warning("Skipping directory '{}'".format(link_src))
        else:
            shutil.copyfile(os.readlink(path), target)
        return True
    else:
        logger.warning(
            '{} will be a dangling symlink'.format(path))
        return False
