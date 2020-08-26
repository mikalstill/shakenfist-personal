# Helpers to resolve images when we don't have an image service

import email.utils
import hashlib
import json
import logging
import os
import re
import requests
import shutil
import time

from oslo_concurrency import processutils

from shakenfist import db
from shakenfist import config
from shakenfist import exceptions
from shakenfist import image_resolver_cirros
from shakenfist import image_resolver_ubuntu
from shakenfist import util


LOG = logging.getLogger(__name__)


resolvers = {
    'cirros': image_resolver_cirros,
    'ubuntu': image_resolver_ubuntu
}

IMAGE_FETCH_LOCK_TIMEOUT = 600   # TODO(andy):Should be linked to HTTP timeout?


def _get_image_lock_name(hashed_image_url):
    return ('sf/images/%s/%s' % (
        config.parsed.get('NODE_NAME'), hashed_image_url))


def get_image(url, locks, op_label, timeout=IMAGE_FETCH_LOCK_TIMEOUT):
    """Fetch image if not downloaded and return image path."""
    hashed_image_url, hashed_image_path = hash_image(url)
    with db.get_lock(_get_image_lock_name(hashed_image_url),
                     timeout=timeout) as image_lock:
        with util.RecordedOperation('fetch image', op_label) as _:
            image_url = resolve(url)
            info, image_dirty, resp = requires_fetch(image_url)

            if image_dirty:
                LOG.info('get_image starting fetch of %s', image_url)
                hashed_image_path = fetch(hashed_image_path, info,
                                          resp, locks=locks.append(image_lock))
            else:
                hashed_image_path = '%s.v%03d' % (
                    hashed_image_path, info['version'])

        _transcode(hashed_image_path, op_label)

    return hashed_image_path


def _transcode(hashed_image_path, op_label):
    with util.RecordedOperation('transcode image', op_label) as _:
        if os.path.exists(hashed_image_path + '.qcow2'):
            return

        current_format = identify(hashed_image_path).get('file format')
        if current_format == 'qcow2':
            os.link(hashed_image_path, hashed_image_path + '.qcow2')
            return

        processutils.execute(
            'qemu-img convert -t none -O qcow2 %s %s.qcow2'
            % (hashed_image_path, hashed_image_path),
            shell=True)


def resolve(name):
    for resolver in resolvers:
        if name.startswith(resolver):
            return resolvers[resolver].resolve(name)
    return name


def _get_cache_path():
    image_cache_path = os.path.join(
        config.parsed.get('STORAGE_PATH'), 'image_cache')
    if not os.path.exists(image_cache_path):
        LOG.debug('Creating image cache at %s' % image_cache_path)
        os.makedirs(image_cache_path)
    return image_cache_path


def hash_image(image_url):
    h = hashlib.sha256()
    h.update(image_url.encode('utf-8'))
    hashed_image_url = h.hexdigest()
    hashed_image_path = os.path.join(_get_cache_path(), hashed_image_url)
    LOG.debug('Image %s hashes to %s' % (image_url, hashed_image_url))
    return hashed_image_url, hashed_image_path


VALIDATED_IMAGE_FIELDS = ['Last-Modified', 'Content-Length']


def _read_info(image_url, hashed_image_url, hashed_image_path):
    if not os.path.exists(hashed_image_path + '.info'):
        info = {
            'url': image_url,
            'hash': hashed_image_url,
            'version': 0
        }
    else:
        with open(hashed_image_path + '.info') as f:
            info = json.loads(f.read())

    return info


def requires_fetch(image_url):
    hashed_image_url, hashed_image_path = hash_image(image_url)
    info = _read_info(image_url, hashed_image_url, hashed_image_path)

    resp = requests.get(image_url, allow_redirects=True, stream=True,
                        headers={'User-Agent': util.get_user_agent()})
    if resp.status_code != 200:
        raise exceptions.HTTPError(
            'Failed to fetch HEAD of %s (status code %d)'
            % (image_url, resp.status_code))

    image_dirty = False
    for field in VALIDATED_IMAGE_FIELDS:
        if info.get(field) != resp.headers.get(field):
            image_dirty = True

    return info, image_dirty, resp


def fetch(hashed_image_path, info, resp, locks=None):
    """Download the image if we don't already have the latest version in cache."""

    def bump_locks(locks):
        if locks:
            for lock in locks:
                if lock:
                    lock.refresh()

    fetched = 0
    info['version'] += 1
    info['fetched_at'] = email.utils.formatdate()
    for field in VALIDATED_IMAGE_FIELDS:
        info[field] = resp.headers.get(field)

    last_lock_refresh = 0
    with open(hashed_image_path + '.v%03d' % info['version'], 'wb') as f:
        for chunk in resp.iter_content(chunk_size=8192):
            fetched += len(chunk)
            f.write(chunk)
            if (time.time() - last_lock_refresh > 10):
                bump_locks(locks)
                last_lock_refresh = time.time()

    if fetched > 0:
        with open(hashed_image_path + '.info', 'w') as f:
            f.write(json.dumps(info, indent=4, sort_keys=True))

        LOG.info('Fetching image %s complete (%d bytes)' %
                 (info['url'], fetched))

    # Decompress if required
    if info['url'].endswith('.gz'):
        if not os.path.exists(hashed_image_path + '.v%03d.orig' % info['version']):
            bump_locks(locks)

            processutils.execute(
                'gunzip -k -q -c %(img)s > %(img)s.orig' % {
                    'img': hashed_image_path + '.v%03d' % info['version']},
                shell=True)
        return '%s.v%03d.orig' % (hashed_image_path, info['version'])

    return '%s.v%03d' % (hashed_image_path, info['version'])


def resize(hashed_image_path, size):
    """Resize the image to the specified size."""

    backing_file = hashed_image_path + '.qcow2' + '.' + str(size) + 'G'

    if os.path.exists(backing_file):
        return backing_file

    current_size = identify(hashed_image_path).get('virtual size')

    if current_size == size * 1024 * 1024 * 1024:
        os.link(hashed_image_path, backing_file)
        return backing_file

    shutil.copyfile(hashed_image_path + '.qcow2', backing_file)
    processutils.execute(
        'qemu-img resize %s %sG' % (backing_file, size),
        shell=True)

    return backing_file


VALUE_WITH_BRACKETS_RE = re.compile(r'.* \(([0-9]+) bytes\)')


def identify(path):
    """Work out what an image is."""

    if not os.path.exists(path):
        return {}

    out, _ = processutils.execute(
        'qemu-img info %s' % path, shell=True)

    data = {}
    for line in out.split('\n'):
        line = line.lstrip().rstrip()
        elems = line.split(': ')
        if len(elems) > 1:
            key = elems[0]
            value = ': '.join(elems[1:])

            m = VALUE_WITH_BRACKETS_RE.match(value)
            if m:
                value = float(m.group(1))

            elif value.endswith('K'):
                value = float(value[:-1]) * 1024
            elif value.endswith('M'):
                value = float(value[:-1]) * 1024 * 1024
            elif value.endswith('G'):
                value = float(value[:-1]) * 1024 * 1024 * 1024
            elif value.endswith('T'):
                value = float(value[:-1]) * 1024 * 1024 * 1024 * 1024

            try:
                data[key] = float(value)
            except Exception:
                data[key] = value

    return data


def create_cow(cache_file, disk_file):
    """Create a COW layer on top of the image cache."""

    if os.path.exists(disk_file):
        return

    processutils.execute(
        'qemu-img create -b %s -f qcow2 %s' % (cache_file, disk_file),
        shell=True)


def create_flat(cache_file, disk_file):
    """Make a flat copy of the disk from the image cache."""

    if os.path.exists(disk_file):
        return

    shutil.copyfile(cache_file, disk_file)


def create_raw(cache_file, disk_file):
    """Make a raw copy of the disk from the image cache."""

    if os.path.exists(disk_file):
        return

    processutils.execute(
        'qemu-img convert -t none -O raw %s %s'
        % (cache_file, disk_file),
        shell=True)


def snapshot(source, destination):
    """Convert a possibly COW layered disk file into a snapshot."""

    processutils.execute(
        'qemu-img convert --force-share -O qcow2 %s %s'
        % (source, destination),
        shell=True)
