#!/usr/bin/env python2

import os
import re
import sys
import struct
import urllib2
import ConfigParser
from errno import ENOENT
from stat import S_IFDIR, S_IFREG
import time
import argparse
import operator
import shutil
import tempfile
import threading
import logging
import pprint

from fuse import FUSE, FuseOSError, Operations, LoggingMixIn, fuse_get_context
from gmusicapi import Mobileclient as GoogleMusicAPI
from gmusicapi import Webclient as GoogleMusicWebAPI

import fifo

reload(sys)  # Reload does the trick
sys.setdefaultencoding('UTF-8')

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger('gmusicfs')
deviceId = None
pp = pprint.PrettyPrinter(indent=4)  # For debug logging

# Size of the ID3v1 trailer appended to the mp3 file (at read time)
# Add the size to the reported size of the mp3 file so read function receive correct params.
# The read function will read size bytes - 128 since we have to generate this 128 bytes.
ID3V1_TRAILER_SIZE = 128


def formatNames(string_from):
    """Format a name to make it suitable to use as a filename"""
    return re.sub('/', '-', string_from)


class NoCredentialException(Exception):
    pass


class Playlist(object):
    """This class manages playlist information"""

    def __init__(self, library, pldata):
        self.library = library
        self.__filename_re = re.compile('^([0-9]+) - [^/]+\.mp3$')

        self.realname = pldata['name']
        self.dirname = formatNames(self.realname).strip()
        log.debug('New playlist: %s' % self.realname)

        self.__tracks = []
        for entry in pldata['tracks']:
            log.debug('Playlist entry: %s' % pp.pformat(entry))
            if 'track' in entry:
                track = entry['track']
                track['id'] = entry['trackId']
            else:
                track = self.library.get_track(entry['trackId'])
            self.__tracks.append(track)

    def get_tracks(self, get_size=False):
        """Return the list of tracks, in order, that comprise the playlist"""
        # TODO Converge implementation by creating a Track class?
        #      It could get the size only on demand per-track
        # Retrieve and remember the filesize of each track:
        if get_size and self.library.true_file_size:
            for t in self.__tracks:
                if 'bytes' not in t:
                    r = urllib2.Request(self.get_track_stream(t)[0])
                    r.get_method = lambda: 'HEAD'
                    u = urllib2.urlopen(r)
                    t['bytes'] = int(u.headers['Content-Length']) + ID3V1_TRAILER_SIZE
        return self.__tracks

    def get_track(self, filename):
        """Return the track that corresponds to a filename from this playlist"""
        m = self.__filename_re.match(filename)
        if m:
            tracknum = int(m.groups()[0])
            return self.__tracks[tracknum - 1]
        return None

    def get_track_stream(self, track):
        """Return the track stream URL"""
        return self.library.api.get_stream_url(track['id'], deviceId)

    def __repr__(self):
        return u'<Playlist \'{name}\'>'.format(name=self.realname)


class Artist(object):
    """This class manages artist information"""

    def __init__(self, library, name):
        self.library = library
        self.realname = name
        self.dirname = formatNames(name)
        self.__albums = {}

    def add_album(self, album):
        """Add an album to the artist"""
        self.__albums[album.normtitle.lower()] = album

    def get_albums(self):
        """Return a list of all the albums by the artist"""
        return self.__albums.values()

    def get_album(self, title):
        """Return a specific album from the set that belongs to the artist"""
        return self.__albums.get(title.lower(), None)

    def __repr__(self):
        return u'<Artist \'{name}\'>'.format(name=self.realname)


class Album(object):
    """This class manages album information"""

    def __init__(self, library, title):
        self.library = library
        self.realtitle = title
        self.normtitle = formatNames(self.realtitle)
        self.__tracks = []
        self.__sorted = True
        self.__filename_re = re.compile("^[0-9]+ - (.*)\.mp3$")

    def add_track(self, track):
        """Add a track to the album"""
        self.__tracks.append(track)
        self.__sorted = False

    def get_tracks(self, get_size=False):
        """Return a sorted list of tracks in the album"""
        # Re-sort by track number
        if not self.__sorted:
            self.__tracks.sort(key=lambda t: t.get('track'))
        # Retrieve and remember the filesize of each track
        if get_size and self.library.true_file_size:
            for t in self.__tracks:
                if 'bytes' not in t:
                    r = urllib2.Request(self.get_track_stream(t))
                    r.get_method = lambda: 'HEAD'
                    u = urllib2.urlopen(r)
                    t['bytes'] = int(u.headers['Content-Length']) + ID3V1_TRAILER_SIZE
        return self.__tracks

    def get_track(self, filename):
        """Get the track name corresponding to a filename
        (eg. '001 - brilliant track name.mp3')"""
        m = self.__filename_re.match(filename)
        if m:
            title = m.groups()[0]
            for track in self.get_tracks():
                if formatNames(track['title'].lower()) == title.lower():
                    return track
        return None

    def get_track_stream(self, track):
        """Return the track stream URL"""
        return self.library.api.get_stream_url(track['id'], deviceId)

    def get_cover_url(self):
        """Return the album cover image URL"""
        try:
            # Assume the first track has the right cover URL
            url = "%s" % self.__tracks[0]['albumArtRef'][0]['url']
        except:
            url = None
        return url

    def get_cover_size(self):
        """Return the album cover size"""
        if self.library.true_file_size:
            r = urllib2.Request(self.get_cover_url())
            r.get_method = lambda: 'HEAD'
            u = urllib2.urlopen(r)
            return int(u.headers['Content-Length'])
        return None

    def get_year(self):
        """Get the year of the album.
        Aggregate all the track years and pick the most popular year
        among them"""
        years = {}  # year -> count
        for track in self.get_tracks():
            y = track.get('year', None)
            if y:
                count = years.get(y, 0)
                years[y] = count + 1
        top_years = sorted(years.items(),
                           key=operator.itemgetter(1), reverse=True)
        try:
            top_year = top_years[0][0]
        except IndexError:
            top_year = 0
        return top_year

    def __repr__(self):
        return u'<Album \'{title}\'>'.format(title=self.normtitle)


class MusicLibrary(object):
    """This class reads information about your Google Play Music library"""

    def __init__(self, username=None, password=None,
                 true_file_size=False, scan=True, verbose=0):
        self.verbose = False
        if verbose > 1:
            self.verbose = True

        self.__login_and_setup(username, password)

        self.__artists = {}  # 'artist name' -> {'album name' : Album(), ...}
        self.__albums = []  # [Album(), ...]
        self.__tracks = {}
        self.__playlists = {}
        if scan:
            self.rescan()
        self.true_file_size = true_file_size

    def rescan(self):
        """Scan the Google Play Music library"""
        self.__artists = {}  # 'artist name' -> {'album name' : Album(), ...}
        self.__albums = []  # [Album(), ...]
        self.__tracks = {}
        self.__playlists = {}
        self.__aggregate_albums()

    def __login_and_setup(self, username=None, password=None):
        # If credentials are not specified, get them from $HOME/.gmusicfs
        if not username or not password:
            cred_path = os.path.join(os.path.expanduser('~'), '.gmusicfs')
            if not os.path.isfile(cred_path):
                raise NoCredentialException(
                    'No username/password was specified. No config file could '
                    'be found either. Try creating %s and specifying your '
                    'username/password there. Make sure to chmod 600.'
                    % cred_path)
            if not oct(os.stat(cred_path)[os.path.stat.ST_MODE]).endswith('00'):
                raise NoCredentialException(
                    'Config file is not protected. Please run: '
                    'chmod 600 %s' % cred_path)
            self.config = ConfigParser.ConfigParser()
            self.config.read(cred_path)
            username = self.config.get('credentials', 'username')
            password = self.config.get('credentials', 'password')
            global deviceId
            deviceId = self.config.get('credentials', 'deviceId')
            if not username or not password:
                raise NoCredentialException(
                    'No username/password could be read from config file'
                    ': %s' % cred_path)
            if not deviceId:
                raise NoCredentialException(
                    'No deviceId could be read from config file'
                    ': %s' % cred_path)
            if deviceId.startswith("0x"):
                deviceId = deviceId[2:]

        self.api = GoogleMusicAPI(debug_logging=self.verbose)
        log.info('Logging in...')
        self.api.login(username, password, deviceId)
        log.info('Login successful.')

    def __aggregate_albums(self):
        """Get all the tracks and playlists in the library, parse into relevant dicts"""
        log.info('Gathering track information...')
        tracks = self.api.get_all_songs()
        for track in tracks:
            log.debug('track = %s' % pp.pformat(track))

            # Prefer the album artist over the track artist if there is one
            artist_name = formatNames(track['albumArtist'])
            if artist_name.strip() == '':
                artist_name = formatNames(track['artist'])
            if artist_name.strip() == '':
                artist_name = 'Unknown'

            # Get the Artist object, or create one if it doesn't exist
            artist = self.__artists.get(artist_name.lower(), None)
            if not artist:
                artist = Artist(self, artist_name)
                self.__artists[artist_name.lower()] = artist

            # Get the Album object, or create one if it doesn't exist
            album = artist.get_album(formatNames(track['album']))
            if not album:
                album = Album(self, track['album'])
                self.__albums.append(album)  # NOTE: Current no purpose other than to count
                artist.add_album(album)

            # Add track to album
            album.add_track(track)

            # Add track to list of all tracks, indexable by track ID
            if 'id' in track:
                self.__tracks[track['id']] = track

        log.debug('%d tracks loaded.' % len(tracks))
        log.debug('%d artists loaded.' % len(self.__artists))
        log.debug('%d albums loaded.' % len(self.__albums))

        # Add all playlists
        playlists = self.api.get_all_user_playlist_contents()
        for pldata in playlists:
            playlist = Playlist(self, pldata)
            self.__playlists[playlist.dirname.lower()] = playlist
        log.debug('%d playlists loaded.' % len(self.__playlists))

    def get_artists(self):
        """Return list of all artists in the library"""
        return self.__artists.values()

    def get_artist(self, name):
        """Return the artist from the library with the specified name"""
        return self.__artists.get(name.lower(), None)

    def get_playlists(self):
        """Return list of all playlists in the library"""
        return self.__playlists.values()

    def get_playlist(self, name):
        """Return the playlist from the library with the specified name"""
        return self.__playlists.get(name.lower(), None)

    def get_track(self, trackid):
        """Return the track from the library with the specified track ID"""
        return self.__tracks.get(trackid, None)

    def cleanup(self):
        pass


class GMusicFS(LoggingMixIn, Operations):
    """Google Music Filesystem"""

    def __init__(self, path, username=None, password=None,
                 true_file_size=False, verbose=0, scan_library=True,
                 lowercase=True):
        Operations.__init__(self)
        self.artist_dir = re.compile('^/artists/(?P<artist>[^/]+)$')
        self.artist_album_dir = re.compile(
            '^/artists/(?P<artist>[^/]+)/(?P<year>[0-9]{4}) - (?P<album>[^/]+)$')
        self.artist_album_track = re.compile(
            '^/artists/(?P<artist>[^/]+)/(?P<year>[0-9]{4}) - (?P<album>[^/]+)/(?P<track>[^/]+\.mp3)$')
        self.artist_album_image = re.compile(
            '^/artists/(?P<artist>[^/]+)/(?P<year>[0-9]{4}) - (?P<album>[^/]+)/(?P<image>[^/]+\.jpg)$')
        self.playlist_dir = re.compile('^/playlists/(?P<playlist>[^/]+)$')
        self.playlist_track = re.compile(
            '^/playlists/(?P<playlist>[^/]+)/(?P<track>[^/]+\.mp3)$')

        self.__open_files = {}  # path -> urllib2_obj

        # Define transformation based on whether lowercase filenames will be used or not
        if lowercase:
            self.transform = lambda x: x.lower()
        else:
            self.transform = lambda x: x

        # Login to Google Play Music and parse the tracks:
        self.library = MusicLibrary(username, password,
                                    true_file_size=true_file_size, verbose=verbose, scan=scan_library)
        log.info("Filesystem ready : %s" % path)

    def cleanup(self):
        self.library.cleanup()

    def track_to_stat(self, track, st={}):
        """Construct and results stat information based on a track"""
        # TODO This could be moved into a Track class in the future
        st['st_mode'] = (S_IFREG | 0444)
        st['st_size'] = int(track['estimatedSize'])
        if 'bytes' in track:
            st['st_size'] = int(track['bytes'])
        st['st_ctime'] = st['st_mtime'] = st['st_atime'] = 0
        if 'creationTimestamp' in track:
            st['st_ctime'] = st['st_mtime'] = int(track['creationTimestamp']) / 1000000
        if 'recentTimestamp' in track:
            st['st_atime'] = int(track['recentTimestamp']) / 1000000
        return st

    def getattr(self, path, fh=None):
        """Get information about a file or directory"""
        artist_dir_m = self.artist_dir.match(path)
        artist_album_dir_m = self.artist_album_dir.match(path)
        artist_album_track_m = self.artist_album_track.match(path)
        artist_album_image_m = self.artist_album_image.match(path)
        playlist_dir_m = self.playlist_dir.match(path)
        playlist_track_m = self.playlist_track.match(path)

        # Default to a directory
        st = {
            'st_mode': (S_IFDIR | 0755),
            'st_nlink': 2}
        date = 0  # Make the date really old, so that cp -u works correctly.
        st['st_ctime'] = st['st_mtime'] = st['st_atime'] = date

        if path == '/':
            pass
        elif path == '/artists':
            pass
        elif path == '/playlists':
            pass
        elif artist_dir_m:
            pass
        elif artist_album_dir_m:
            pass
        elif artist_album_track_m:
            parts = artist_album_track_m.groupdict()
            artist = self.library.get_artist(parts['artist'])
            album = artist.get_album(parts['album'])
            track = album.get_track(parts['track'])
            st = self.track_to_stat(track)
        elif artist_album_image_m:
            parts = artist_album_image_m.groupdict()
            artist = self.library.get_artist(parts['artist'])
            album = artist.get_album(parts['album'])
            cover_size = album.get_cover_size()
            if cover_size is None:
                cover_size = 10000000
            st = {
                'st_mode': (S_IFREG | 0444),
                'st_size': cover_size}
        elif playlist_dir_m:
            pass
        elif playlist_track_m:
            parts = playlist_track_m.groupdict()
            playlist = self.library.get_playlist(parts['playlist'])
            track = playlist.get_track(parts['track'])
            st = self.track_to_stat(track)
        else:
            raise FuseOSError(ENOENT)

        return st

    def open(self, path, fh):
        """Open a file (track or cover image) and return a filehandle"""

        artist_album_track_m = self.artist_album_track.match(path)
        artist_album_image_m = self.artist_album_image.match(path)
        playlist_track_m = self.playlist_track.match(path)

        if artist_album_track_m:
            parts = artist_album_track_m.groupdict()
            artist = self.library.get_artist(parts['artist'])
            album = artist.get_album(parts['album'])
            track = album.get_track(parts['track'])
            url = album.get_track_stream(track)
        elif artist_album_image_m:
            parts = artist_album_image_m.groupdict()
            artist = self.library.get_artist(parts['artist'])
            album = artist.get_album(parts['album'])
            url = album.get_cover_url()
        elif playlist_track_m:
            parts = playlist_track_m.groupdict()
            playlist = self.library.get_playlist(parts['playlist'])
            track = playlist.get_track(parts['track'])
            url = self.library.api.get_stream_url(track['id'], deviceId)
        else:
            RuntimeError('unexpected opening of path: %r' % path)

        u = self.__open_files[fh] = urllib2.urlopen(url)
        u.bytes_read = 0

        return fh

    def release(self, path, fh):
        u = self.__open_files.get(fh, None)
        if u:
            u.close()
            del self.__open_files[fh]

    def read(self, path, size, offset, fh):
        u = self.__open_files.get(fh, None)
        if u is None:
            raise RuntimeError('unexpected path: %r' % path)
        artist_album_track_m = self.artist_album_track.match(path)
        if artist_album_track_m and (int(u.headers['Content-Length']) < (offset + size)):
            parts = artist_album_track_m.groupdict()
            artist = self.library.get_artist(parts['artist'])
            album = artist.get_album(parts['album'])
            track = album.get_track(parts['track'])
            # Genre tag is always set to Other as Google MP3 genre tags are not id3v1 id.
            id3v1 = struct.pack("!3s30s30s30s4s30sb", 'TAG', str(track['title']), str(track['artist']),
                            str(track.get('album', '')), str(0), str(track.get('comment', '')), 12)
            buf = u.read(size - ID3V1_TRAILER_SIZE) + id3v1
        else:
            buf = u.read(size)

        try:
            u.bytes_read += size
        except AttributeError:
            # Only urllib2 files need this attribute, harmless to
            # ignore it.
            pass
        return buf

    def readdir(self, path, fh):
        artist_dir_m = self.artist_dir.match(path)
        artist_album_dir_m = self.artist_album_dir.match(path)
        artist_album_track_m = self.artist_album_track.match(path)
        artist_album_image_m = self.artist_album_image.match(path)
        playlist_dir_m = self.playlist_dir.match(path)

        if path == '/':
            return ['.', '..', 'artists', 'playlists']
        elif path == '/artists':
            artist_dirs = map((lambda a: self.transform(a.dirname)), self.library.get_artists())
            return ['.', '..'] + artist_dirs
        elif path == '/playlists':
            playlist_dirs = map((lambda p: self.transform(p.dirname)), self.library.get_playlists())
            return ['.', '..'] + playlist_dirs
        elif artist_dir_m:
            # Artist directory, lists albums.
            parts = artist_dir_m.groupdict()
            artist = self.library.get_artist(parts['artist'])
            albums = artist.get_albums()
            # Sort albums by year:
            album_dirs = [u'{year:04d} - {name}'.format(
                year=a.get_year(), name=self.transform(a.normtitle)) for a in albums]
            return ['.', '..'] + album_dirs
        elif artist_album_dir_m:
            # Album directory, lists tracks.
            parts = artist_album_dir_m.groupdict()
            artist = self.library.get_artist(parts['artist'])
            album = artist.get_album(parts['album'])
            files = ['.', '..']
            for track in album.get_tracks(get_size=True):
                files.append('%03d - %s.mp3' %
                    (track['trackNumber'], self.transform(formatNames(track['title']))))
            # Include cover image:
            cover = album.get_cover_url()
            if cover:
                files.append('cover.jpg')
            return files
        elif playlist_dir_m:
            parts = playlist_dir_m.groupdict()
            playlist = self.library.get_playlist(parts['playlist'])
            files = ['.', '..']
            tracknum = 1
            for track in playlist.get_tracks():
                files.append(self.transform(formatNames('%03d - %s - %s - %s.mp3' % (tracknum, track['artist'], track['album'], track['title']))))
                tracknum += 1
            return files


def getDeviceId(verbose=False):
    cred_path = os.path.join(os.path.expanduser('~'), '.gmusicfs')
    if not os.path.isfile(cred_path):
        raise NoCredentialException(
            'No username/password was specified. No config file could '
            'be found either. Try creating %s and specifying your '
            'username/password there. Make sure to chmod 600.'
            % cred_path)
    if not oct(os.stat(cred_path)[os.path.stat.ST_MODE]).endswith('00'):
        raise NoCredentialException(
            'Config file is not protected. Please run: '
            'chmod 600 %s' % cred_path)
    config = ConfigParser.ConfigParser()
    config.read(cred_path)
    username = config.get('credentials', 'username')
    password = config.get('credentials', 'password')
    if not username or not password:
        raise NoCredentialException(
            'No username/password could be read from config file'
            ': %s' % cred_path)

    api = GoogleMusicWebAPI(debug_logging=verbose)
    log.info('Logging in...')
    api.login(username, password)
    log.info('Login successful.')

    for device in api.get_registered_devices():
        if not device['name']:
            device['name'] = 'NoName'
        if device['id'][1] == 'x':
            print '%s : %s' % (device['name'], device['id'])


def main():
    log.setLevel(logging.WARNING)
    logging.getLogger('gmusicapi').setLevel(logging.WARNING)
    logging.getLogger('fuse').setLevel(logging.WARNING)
    logging.getLogger('requests.packages.urllib3').setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description='GMusicFS', add_help=False)
    parser.add_argument('--deviceid', action='store_true', dest='deviceId')

    args = parser.parse_known_args()

    if args[0].deviceId:
        getDeviceId()
        return

    parser = argparse.ArgumentParser(description='GMusicFS')
    parser.add_argument('mountpoint', help='The location to mount to')
    parser.add_argument('-f', '--foreground', dest='foreground',
                        action="store_true",
                        help='Don\'t daemonize, run in the foreground.')
    parser.add_argument('-v', '--verbose', help='Be a little verbose',
                        action='store_true', dest='verbose')
    parser.add_argument('-vv', '--veryverbose', help='Be very verbose',
                        action='store_true', dest='veryverbose')
    parser.add_argument('-t', '--truefilesize', help='Report true filesizes'
                        ' (slower directory reads)',
                        action='store_true', dest='true_file_size')
    parser.add_argument('--allusers', help='Allow all system users access to files'
                        ' (Requires user_allow_other set in /etc/fuse.conf)',
                        action='store_true', dest='allusers')
    parser.add_argument('--nolibrary', help='Don\'t scan the library at launch',
                        action='store_true', dest='nolibrary')
    parser.add_argument('--deviceid', help='Get the device ids bounded to your account',
                        action='store_true', dest='deviceId')
    parser.add_argument('-l', '--lowercase', help='Convert all path elements to lowercase',
                        action='store_true', dest='lowercase')

    args = parser.parse_args()

    mountpoint = os.path.abspath(args.mountpoint)

    # Set verbosity:
    if args.veryverbose:
        log.setLevel(logging.DEBUG)
        logging.getLogger('gmusicapi').setLevel(logging.DEBUG)
        logging.getLogger('fuse').setLevel(logging.DEBUG)
        logging.getLogger('requests.packages.urllib3').setLevel(logging.WARNING)
        verbosity = 10
    elif args.verbose:
        log.setLevel(logging.INFO)
        logging.getLogger('gmusicapi').setLevel(logging.WARNING)
        logging.getLogger('fuse').setLevel(logging.INFO)
        logging.getLogger('requests.packages.urllib3').setLevel(logging.WARNING)
        verbosity = 1
    else:
        log.setLevel(logging.WARNING)
        logging.getLogger('gmusicapi').setLevel(logging.WARNING)
        logging.getLogger('fuse').setLevel(logging.WARNING)
        logging.getLogger('requests.packages.urllib3').setLevel(logging.WARNING)
        verbosity = 0

    fs = GMusicFS(mountpoint, true_file_size=args.true_file_size, verbose=verbosity, scan_library=not args.nolibrary, lowercase=args.lowercase)
    try:
        fuse = FUSE(fs, mountpoint, foreground=args.foreground,
                    ro=True, nothreads=True, allow_other=args.allusers)
    finally:
        fs.cleanup()

if __name__ == '__main__':
    main()
