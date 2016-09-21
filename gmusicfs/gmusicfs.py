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

from eyed3.id3 import Tag, ID3_V2_4
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn, fuse_get_context
import gmusicapi.exceptions
from gmusicapi import Mobileclient as GoogleMusicAPI
from gmusicapi import Webclient as GoogleMusicWebAPI

import fifo

reload(sys)  # Reload does the trick
sys.setdefaultencoding('UTF-8')

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger('gmusicfs')
deviceId = None
pp = pprint.PrettyPrinter(indent=4)  # For debug logging

ALBUM_REGEX = '(?P<album>[^/]+) \((?P<year>[0-9]{4})\)'
ALBUM_FORMAT = u'{name} ({year:04d})'

TRACK_REGEX = '(?P<track>(?P<number>[0-9]+) - (?P<title>.*)\.mp3)'
TRACK_FORMAT = '{number:02d} - {name}.mp3'

def formatNames(string_from):
    """Format a name to make it suitable to use as a filename"""
    return re.sub('/', '-', string_from)


class NoCredentialException(Exception):
    pass


class Playlist(object):
    """This class manages playlist information"""

    def __init__(self, library, pldata):
        self.library = library
        self.__filename_re = re.compile(TRACK_REGEX)

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

        ID3V1_TRAILER_SIZE = 128
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
            tracknum = int(m.group('title'))
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
        self.__filename_re = re.compile(TRACK_REGEX)
        self.__art = None
        self.__art_size = None
        self.__art_url = None
        self.__discs = []

    def gen_tag(self, track, fake_art=False):
        tag = Tag()

        if track.has_key('album'):
            tag.album = track['album']
        if track.has_key('artist'):
            tag.artist = track['artist']
        if track.has_key('title'):
            tag.title = track['title']
        if track.has_key('discNumber'):
            tag.disc_num = int(track['discNumber'])
        if track.has_key('trackNumber'):
            tag.track_num = int(track['trackNumber'])
        if track.has_key('genre'):
            tag.genre = track['genre']
        if track.has_key('albumArtist') and track['albumArtist'] != track['artist']:
            tag.album_artist = track['albumArtist']
        if track.has_key('year') and int(track['year']) != 0:
            tag.recording_date = track['year']
        if track.has_key('albumArtRef'):
            art = None
            if self.__art is None:
                if fake_art:
                    art = '\0' * self.__art_size
                else:
                    if self.load_art():
                        art = self.__art
                    else:
                        art = None
            else:
                art = self.__art
            if art is not None:
                tag.images.set(0x03, art, 'image/jpeg', u'Front cover')
        return tag

    def render_tag(self, tag):
        tmpfd, tmpfile = tempfile.mkstemp()
        os.close(tmpfd)
        tag.save(tmpfile, ID3_V2_4)
        tmpfd = open(tmpfile, "r")
        rendered_tag = tmpfd.read()
        tmpfd.close()
        os.unlink(tmpfile)
        return rendered_tag

    def calc_size(self, track):
        if not track.has_key('tagSize'):
            if self.__art_url is None and track.has_key('albumArtRef'):
                self.__art_url = "%s" % track['albumArtRef'][0]['url']
                r = urllib2.Request(self.__art_url)
                r.get_method = lambda: 'HEAD'
                u = urllib2.urlopen(r)
                self.__art_size = int(u.headers['Content-Length'])
                u.close()

            tag = self.gen_tag(track, fake_art=True)
            id3data = self.render_tag(tag)
            track['tagSize'] = str(int(track['estimatedSize']) + len(id3data))
            del id3data
            for frame in tag.frame_set.getAllFrames():
                if hasattr(frame, 'text'):
                    log.debug(frame.id, frame.text)
                else:
                    log.debug(frame.id)
            del tag
            for tnum in range(0, len(self.__tracks)):
                if self.__tracks[tnum]['id'] == track['id']:
                    self.__tracks[tnum]['tagSize'] = track['tagSize']
        return track

    def add_track(self, track):
        """Add a track to the album"""

        self.__tracks.append(track)
        self.__sorted = False

    def load_art(self):
        if self.__art_url is not None:
            u = urllib2.urlopen(self.__art_url)
            self.__art = ""
            data = u.read()
            while data != "":
                self.__art += data
                data = u.read()
            return True
        else:
            return False

    def get_tracks(self, get_size=False):
        # Re-sort by track number:
        if not self.__sorted:
            self.__tracks.sort(key=lambda t: t.get('track'))
        return self.__tracks

    def get_track(self, filename):
        """Get the track name corresponding to a filename
        (eg. '001 - brilliant track name.mp3')"""

        m = self.__filename_re.match(filename)
        if m:
            title = m.group('title')
            for track in self.get_tracks():
                if formatNames(track['title'].lower()) == title.lower():
                    return track
        return None

    def get_track_stream(self, track):
        """Return the track stream URL"""

        return self.library.api.get_stream_url(track['id'], deviceId)

    def get_track_count(self):
        return len(self.__tracks)

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
        self.__gartists = {}
        self.__albums = []  # [Album(), ...]
        self.__galbums = {}
        self.__tracks = {}
        self.__playlists = {}
        if scan:
            self.rescan()
        self.true_file_size = true_file_size

    def rescan(self):
        """Scan the Google Play Music library"""
        self.__artists = {}  # 'artist name' -> {'album name' : Album(), ...}
        self.__gartists = {}
        self.__albums = []  # [Album(), ...]
        self.__galbums = {}
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
            deviceId = self.config.get('credentials', 'deviceId') || GoogleMusicAPI.FROM_MAC_ADDRESS
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

    def __set_key_from_ginfo(self, track, ginfo, key, to_key=None):
        """Set track key from either album_info or artist_info"""
        if to_key is None:
            to_key = key

        try:
            int_key = int(key)
        except ValueError:
            int_key = None

        if (not track.has_key(key) or track[key] == "" or int_key == 0) and ginfo.has_key(to_key):
            track[key] = ginfo[to_key]

        return track

    def __aggregate_albums(self):
        """Get all the tracks and playlists in the library, parse into relevant dicts"""
        log.info('Gathering track information...')
        tracks = self.api.get_all_songs()
        for track in tracks:
            log.debug('track = %s' % pp.pformat(track))

            # Get album and artist information from Google
            if track.has_key('albumId'):
                if self.__galbums.has_key(track['albumId']):
                    album_info = self.__galbums[track['albumId']]
                else:
                    log.info("Downloading album info for %s '%s'", track['albumId'], track['album'])
                    try:
                        album_info = self.__galbums[track['albumId']] = self.api.get_album_info(track['albumId'], include_tracks=False)
                    except gmusicapi.exceptions.CallFailure:
                        log.exception("Failed to download album info for %s '%s'", track['albumId'], track['album'])
                        album_info = {}
                if album_info.has_key('artistId') and len(album_info['artistId']) > 0 and album_info['artistId'][0] != "":
                    artist_id = album_info['artistId'][0]
                    if self.__gartists.has_key(artist_id):
                        artist_info = self.__gartists[artist_id]
                    else:
                        log.info("Downloading artist info for %s '%s'", artist_id, album_info['albumArtist'])
                        #if album_info['albumArtist'] == "Various":
                        #    print album_info
                        artist_info = self.__gartists[artist_id] = self.api.get_artist_info(artist_id, include_albums=False, max_top_tracks=0, max_rel_artist=0)
                else:
                    artist_info = {}
            else:
                album_info = {}
                artist_info = {}

            track = self.__set_key_from_ginfo(track, album_info, 'album', 'name')
            track = self.__set_key_from_ginfo(track, album_info, 'year')
            track = self.__set_key_from_ginfo(track, artist_info, 'albumArtist', 'name')

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

        log.info('%d tracks loaded.' % len(tracks))
        log.info('%d artists loaded.' % len(self.__artists))
        log.info('%d albums loaded.' % len(self.__albums))

        # Add all playlists
        playlists = self.api.get_all_user_playlist_contents()
        for pldata in playlists:
            playlist = Playlist(self, pldata)
            self.__playlists[playlist.dirname.lower()] = playlist
        log.debug('%d playlists loaded.' % len(self.__playlists))

    def get_artists(self):
        """Return all artists in the library"""
        return self.__artists

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

        artist = '/artists/(?P<artist>[^/]+)'

        self.artist_dir = re.compile('^{artist}$'.format(
            artist=artist))
        self.artist_album_dir = re.compile('^{artist}/{album}$'.format(
            artist=artist, album=ALBUM_REGEX))
        self.artist_album_track = re.compile('^{artist}/{album}/{track}$'.format(
            artist=artist, album=ALBUM_REGEX, track=TRACK_REGEX))

        self.playlist_dir = re.compile('^/playlists/(?P<playlist>[^/]+)$')
        self.playlist_track = re.compile(
            '^/playlists/(?P<playlist>[^/]+)/(?P<track>[^/]+\.mp3)$')

        self.__open_files = {}  # path -> urllib2_obj
        self.__urls = {}        # path -> url
        self.__tags = {}        # fh -> (id3v1, id3v2)

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

        st['st_mode'] = (S_IFREG | 0o444)
        st['st_nlink'] = 1
        st['st_size'] = int(track['tagSize'])
        if 'bytes' in track:
            st['st_size'] = int(track['bytes'])
        st['st_ctime'] = st['st_mtime'] = st['st_atime'] = 0
        if 'creationTimestamp' in track:
            st['st_ctime'] = st['st_mtime'] = int(track['creationTimestamp']) / 1000000
        if 'recentTimestamp' in track:
            st['st_atime'] = int(track['recentTimestamp']) / 1000000
        return st
    
    listxattr = None

    def getattr(self, path, fh=None):
        """Get information about a file or directory"""

        artist_dir_m = self.artist_dir.match(path)
        artist_album_dir_m = self.artist_album_dir.match(path)
        artist_album_track_m = self.artist_album_track.match(path)
        playlist_dir_m = self.playlist_dir.match(path)
        playlist_track_m = self.playlist_track.match(path)

        # Default to a directory
        st = {
            'st_mode': (S_IFDIR | 0o755),
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
            parts = artist_album_dir_m.groupdict()
            artist = self.library.get_artist(parts['artist'])
            try:
                album = artist.get_album(parts['album'])
            except:
                log.exception('Failed to get album "%s" for artist "%s"',
                    parts['album'], parts['artist'])
                raise FuseOSError(ENOENT)
            st['st_size'] = album.get_track_count()
        elif artist_album_track_m:
            parts = artist_album_track_m.groupdict()
            artist = self.library.get_artist(parts['artist'])
            album = artist.get_album(parts['album'])
            try:
                track = album.get_track(parts['track'])
            except:
                log.exception('Failed to get track "%s" from album "%s" for artist "%s"',
                    parts['track'], parts['album'], parts['artist'])
                raise FuseOSError(ENOENT)
            if not track.has_key('tagSize'):
                track = album.calc_size(track)
            st = self.track_to_stat(track)
        elif playlist_dir_m:
            pass
        elif playlist_track_m:
            parts = playlist_track_m.groupdict()
            playlist = self.library.get_playlist(parts['playlist'])
            track = playlist.get_track(parts['track'])
            st = self.track_to_stat(track)
        else:
            raise FuseOSError(ENOENT)

            if not track.has_key('tagSize'):
                track = album.calc_size(track)
        return st

    getxattr = None
    
    def _open(self, path, fh):
        album_track = self.__urls.get(fh, None)
        if album_track is None:
            raise RuntimeError('unexpected path: %r' % path)
        (album, track) = album_track
        url = album.get_track_stream(track)
        u = self.__open_files[fh] = urllib2.urlopen(url)
        u.bytes_read = 0
        return fh

    def open(self, path, fh):
        artist_album_track_m = self.artist_album_track.match(path)

        if artist_album_track_m:
            parts = artist_album_track_m.groupdict()
            artist = self.library.get_artist(parts['artist'])
            album = artist.get_album(parts['album'])
            track = album.get_track(parts['track'])
            if not track.has_key('tagSize'):
                track = album.calc_size(track)

            self.__urls[fh] = (album, track)
            self.__tags[fh] = (album.gen_tag(track))
        else:
            RuntimeError('unexpected opening of path: %r' % path)

        return fh

    def release(self, path, fh):
        u = self.__open_files.get(fh, None)
        if u:
            u.close()
            del self.__open_files[fh]

    def read(self, path, size, offset, fh):
        album_track = self.__urls.get(fh, None)
        if album_track is None:
            raise RuntimeError('unexpected path: %r' % path)
        (album, track) = album_track

        id3v2data = ''
        tag = self.__tags.get(fh, None)
        if tag:
            id3v2data = album.render_tag(tag)

        end_id3v2tag = len(id3v2data)
        buf = ''

        if offset < end_id3v2tag:
            buf = id3v2data[offset:offset+size]
            size -= len(buf)
            offset = end_id3v2tag

        if size <= 0:
            return buf

        u = self.__open_files.get(fh, None)
        if u is None:
            if self.__urls.get(fh, None) is None:
                raise RuntimeError('unexpected path: %r' % path)
            else:
                self._open(path, fh)
                u = self.__open_files.get(fh, None)
                if u is None:
                    raise RuntimeError('unexpected path: %r' % path)

        temp_buf = u.read(size)
        if len(temp_buf) < size:
            diff = size - len(temp_buf)
            temp_buf += '\0' * diff
        buf += temp_buf
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
        playlist_dir_m = self.playlist_dir.match(path)

        if path == '/':
            return ['.', '..', 'artists', 'playlists']
        elif path == '/artists':
            artist_dirs = []
            artist_dirs = map((lambda a: self.transform(a.dirname)), self.library.get_artists().values())
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
            album_dirs = [ALBUM_FORMAT.format(
                year=a.get_year(), name=self.transform(a.normtitle)) for a in albums]
            return ['.', '..'] + album_dirs
        elif artist_album_dir_m:
            # Album directory, lists tracks.
            parts = artist_album_dir_m.groupdict()
            artist = self.library.get_artist(parts['artist'])
            album = artist.get_album(parts['album'])
            files = ['.', '..']
            for track in album.get_tracks(get_size=True):
                files.append(TRACK_FORMAT.format(
                    number=track['trackNumber'], artist=track['artist'],
                    name=self.transform(formatNames(track['title']))))
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
    parser.add_argument('--allow_other', help='Allow all system users access to files'
                        ' (Requires user_allow_other set in /etc/fuse.conf)',
                        action='store_true', dest='allow_other')
    parser.add_argument('--allow_root', help='Allow root access to files',
                        action='store_true', dest='allow_root')
    parser.add_argument('--uid', help='Set filesystem uid (numeric)', default=os.getuid(),
                        action='store', dest='uid')
    parser.add_argument('--gid', help='Set filesystem gid (numeric)', default=os.getgid(),
                        action='store', dest='gid')
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
        logging.getLogger('gmusicapi').setLevel(logging.INFO)
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
                    ro=True, nothreads=True, allow_other=args.allow_other, allow_root=args.allow_root, uid=args.uid, gid=args.gid)
    finally:
        fs.cleanup()

if __name__ == '__main__':
    main()
