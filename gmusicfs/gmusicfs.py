#!/usr/bin/env python3

import argparse
import configparser
import inspect
import logging
import os
import pprint
import re
import sys
import tempfile
import traceback
import urllib

import magic
mime = magic.Magic(mime=True)

from errno import ENOENT
from stat import S_IFDIR, S_IFREG

from eyed3.id3 import Tag, ID3_V2_4
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn

import gmusicapi.exceptions

from gmusicapi import Mobileclient as GoogleMusicAPI
# from gmusicapi import Webclient as GoogleMusicWebAPI

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger('gmusicfs')
pp = pprint.PrettyPrinter(indent=2)  # For debug logging

ALBUM_REGEX  = '(?P<album>[^/]+) \((?P<year>[0-9]{4})\)'
ALBUM_FORMAT = "{0.title_printable} ({0.year:04d})"

# TODO: check:
TRACK_REGEX  = '(?P<disc>[0-9]+)-(?P<track>(?P<number>[0-9]+)) - (?P<trartist>.+?) - ((?P<title>.*)\.mp3)'
TRACK_FORMAT = "{0.disk:02d}-{0.number:02d} - {0.artist_printable} - {0.title_printable}.mp3"

ID3V1_TRAILER_SIZE = 128

NO_ALBUM_TITLE  = "No Album"
NO_ARTIST_TITLE = "No Artist"


def format_to_path(string_from):
    """Format a name to make it suitable to use as a filename"""
    return re.sub('[^\w0-9_\.!?#@$ ]+', '_', string_from.strip())


class NoCredentialException(Exception):
    pass


class Artist(object):
    def __init__(self, library, data, name = None):
        self.__library = library
        self.__albums  = {}
        self.__data    = data

        # name
        if name is None and 'artist' in data:
            self.__name = data['artist']
        elif name is not None:
            self.__name = name
        else:
            self.__name = NO_ARTIST_TITLE

        # name_printable
        self.__name_printable = format_to_path(self.__name)

        # artist_id
        if 'artistId' in data:
            self.__id = data['artistId']
        else:
            self.__id = self.__name



    @property
    def id(self):
        return self.__id

    @property
    def name(self):
        return self.__name

    @property
    def name_printable(self):
        return self.__name_printable

    @property
    def albums(self):
        return self.__albums

    def add_album(self, album):
        if album.title_printable not in self.__albums:
            self.__albums[album.title_printable] = album

    # TODO: check it
    def __str__(self):
        return "{0.name_printable}".format(self)


class Album(object):


    def __init__(self, library, data):
        self.__library    = library
        self.__data       = data
        self.__tracks     = {}
        self.__artists    = {}
        self.__art        = bytes()
        self.__art_mime   = "FTW MIME TYPE?"
        self.__album_info = None

        # album_title
        if 'album' in data and data['album'].strip() != "":
            self.__album_title = data['album']
        else:
            self.__album_title = NO_ALBUM_TITLE

        # album_title_printable
        self.__album_title_printable = format_to_path(self.__album_title)

        # album_artist ## self.__artist = self.__library.artists.get(data['artistId'][0], None)
        if 'albumArtist' in data and data['albumArtist'].strip() != "":
            self.__album_artist = data['albumArtist']
        elif 'artist' in data and data['artist'].strip() != "":
            self.__album_artist = data['artist']
        else:
            self.__album_artist = NO_ARTIST_TITLE

        # album_artist_printable
        self.__album_artist_printable = format_to_path(self.__album_artist)

        # album_id
        if 'albumId' in data:
            self.__id = data['albumId']
        elif format_to_path(self.__album_title) == format_to_path(NO_ALBUM_TITLE):
            self.__id = self.__album_title + self.__album_artist  #TODO: check it
        else:
            self.__id = self.__album_title

        # year
        if 'year' in data:
            self.__year = data['year']
        else:
            self.__year = 0

        if 'albumArtRef' in data:
            self.__art_url = data['albumArtRef'][0]['url']
        else:
            self.__art_url = None

        if 'artist' in data:
            self.__artist = data['artist']
        else:
            self.__artist = self.__album_artist

        if 'artistId' in data:
            self.__artistId = data['artistId']
        else:
            self.__artistId = self.__album_artist

    @property
    def id(self):
        return self.__id

    @property
    def tracks(self):
        return self.__tracks

    @property
    def title(self):
        return self.__album_title

    @property
    def title_printable(self):
        return self.__album_title_printable

    @property
    def year(self):
        if not self.__year:
            self.__get_year()
        return self.__year

    @property
    def album_artist(self):
        return self.__album_artist

    @property
    def album_artist_printable(self):
        return self.__album_artist_printable

    @property
    def art(self):
        if not self.__art:
            self.__load_art()
        return self.__art

    @property
    def art_mime(self):
        return self.__art_mime

    def add_track(self, track):
        self.__tracks[str(track)] = track
        if track.id not in self.__library.tracks:  # TODO: may be from playlists check it
            self.__library.tracks[track.id] = track

    def add_artist(self, artist):
        self.__artists[artist.name_printable] = artist

    def __get_year(self):
        # some tracks are not loaded from album_info, let's use them to get the album date release
        for track in self.__tracks.values():
            self.__year = track.year or self.__year

    # TODO: need check image type! some tracks has png cover
    def __load_art(self):
        if not self.__art_url:
            return
        log.info("loading art album: {0.title}".format(self))
        self.__art = bytes()
        art_path   = os.path.join(os.path.expanduser('~'), '.gmusicfs', 'album_arts', self.id + ".jpg")

        try:
            localArt   = open(art_path, "rb")
        except:
            localArt = ""

        if localArt:
            print("# ART FROM FS")
            data = localArt.read()
            while data:
                self.__art += data
                data = localArt.read()
            localArt.close()
        else:
            print("# ART FROM URL")
            u = urllib.request.urlopen(self.__art_url)
            # TODO: wtf?
            data = u.read()
            while data:
                self.__art += data
                data = u.read()
            u.close()

            print("# ART WRITE TO DISK")
            localArt = open(art_path, "wb")
            localArt.write(self.__art)
            localArt.close()

        self.__art_mime = mime.from_buffer(self.__art)

        print("loading art album: {0.title_printable} {0.art_mime}".format(self) + " done!")

    def __str__(self):
        title2 = ALBUM_FORMAT.format(self)
        return title2


class Track(object):

    @property
    def id(self):
        return self.__id

    @property
    def title(self):
        return self.__title.strip()

    @property
    def title_printable(self):
        return self.__title_printable

    @property
    def number(self):
        return self.__number

    @property
    def disk(self):
        return self.__disc

    @property
    def album(self):
        return self.__album

    @property
    def album_printable(self):
        return self.__album_printable

    @property
    def artist(self):
        return self.__artist

    @property
    def artist_printable(self):
        return self.__artist_printable

    @property
    def album_artist(self):
        return self.__album_artist

    @property
    def album_artist_printable(self):
        return self.__album_artist_printable

    @property
    def year(self):
        return self.__year

    def __init__(self, library, data):
        self.__library      = library
        self.__data         = data

        self.__artists      = {}
        self.__albums       = {}
        self.__stream_url   = None
        self.__stream_cache = bytes()
        self.__rendered_tag = bytes()
        self.__tag          = ""

        # TODO: I need to figure out where this is used
        # track_id
        if 'track' in data:
            self.__id = data['trackId']
            print("# track use trackId")
        elif 'id' in data:
            self.__id = data['id']
            print("# track use id")
        elif 'storeId' in data:
            self.__id = data['storeId']
            print("# track use storeId")
        else:
            self.__id = data['nid']
            print("# track use nid")

        # track_title
        if 'title' in data:
            self.__title = data['title']
        else:
            self.__title = "unknown_track_" + data['id']
            print("# track has no title")

        # track_title_printable
        self.__title_printable = format_to_path(self.__title)

        # track_number
        if 'trackNumber' in data:
            self.__number = data['trackNumber']
        else:
            self.__number = 0
            print("# track has no track num")

        #track_disk
        if 'discNumber' in data:
            self.__disc = data['discNumber']
        else:
            self.__disc = 0
            print("# track has no discNumber")

        #track_artist
        if 'artist' in data and data['artist'].strip() != "":
            self.__artist = data['artist']
        elif 'albumArtist' in data and data['albumArtist'].strip() != "":
            self.__artist = data['albumArtist']
            print("# track artist from albumArtist")  # TODO: needed??
        else:
            self.__artist = NO_ARTIST_TITLE
            print("# track has no artist")

        # track_artist_printable
        self.__artist_printable = format_to_path(self.__artist)

        # track_album
        if 'album' in data and data['album'].strip() != "":
            self.__album = data['album']
        else:
            self.__album = NO_ALBUM_TITLE
            print("# track has no album")

        # track_artist_printable
        self.__album_printable = format_to_path(self.__album)

        # album_artist
        if 'albumArtist' in data and data['albumArtist'].strip() != "":
            self.__album_artist = data['albumArtist']
        else:
            self.__album_artist = ""
            print("# track has no albumArtist")

        # track_artist_printable
        self.__album_artist_printable = format_to_path(self.__album_artist)


        if 'year' in data:
            self.__year = data['year']
        else:
            self.__year = 0
            print("# track has no year")

    def add_album(self, album):  # TODO: check before insert? may be move outside check in class????
        self.__albums[album.title_printable] = album

    def add_artist(self, artist):  # TODO: check before insert? may be move outside check in class????
        self.__artists[artist.name_printable] = artist

    def __gen_tag(self):
        print("Creating tag idv3...")
        self.__tag           = Tag()
        self.__tag.album     = self.album
        self.__tag.artist    = self.artist
        self.__tag.title     = self.title
        self.__tag.disc_num  = int(self.disk)
        self.__tag.track_num = int(self.number)

        if 'genre' in self.__data:
            self.__tag.genre = self.__data['genre']

        if self.album_artist != 'genre':
            self.__tag.album_artist = self.album_artist

        if int(self.year) != 0:
            self.__tag.recording_date = self.year


        if self.album_printable in self.__albums and self.__albums[self.album_printable].art:
            mime_type = self.__albums[self.album_printable].art_mime #TODO: check mimetype
            self.__tag.images.set(0x03, self.__albums[self.album_printable].art, mime_type, u'Front cover')

        else:
            print("# track has no art?")

        #  ###### pp.pprint(self.__tag.__dict__)

        # TODO: niggercode??
        tmpfd, tmpfile = tempfile.mkstemp()
        os.close(tmpfd)
        self.__tag.save(tmpfile, ID3_V2_4)
        tmpfd = open(tmpfile, "rb")
        self.__rendered_tag = tmpfd.read()

        pp.pprint(self.__rendered_tag)

        tmpfd.close()
        os.unlink(tmpfile)



    def get_attr(self):

        st = {'st_mode': (S_IFREG | 0o777), 'st_nlink': 1, 'st_ctime': 0, 'st_mtime': 0, 'st_atime': 0}
        # todo: need good data

        if 'bytes' in self.__data:
            st['st_size'] = int(self.__data['bytes'])
        elif 'estimatedSize' in self.__data:
            st['st_size'] = int(self.__data['estimatedSize'])
        else:
            if 'tagSize' in self.__data:
                st['st_size'] = int(self.__data['tagSize'])  # wtf??
            else:
                print("####tagSize... wtf?")
                st['st_size'] = 0
                st['st_mode'] = (S_IFREG | 0o000)

        if 'creationTimestamp' in self.__data:
            st['st_ctime'] = st['st_mtime'] = int(self.__data['creationTimestamp']) / 1000000

        if 'recentTimestamp' in self.__data:
            st['st_atime'] = int(self.__data['recentTimestamp']) / 1000000

        return st

    def _open(self):
        print("#######file openned!")
        print(self.__data)
        pass
        # self.__stream_url = urllib.request.urlopen(self.__library.get_stream_url(self.id))
        # self.__stream_cache += self.__stream_url.read(64*1024) # Some caching

    def read(self, read_offset, read_chunk_size):

        print("RUN READ: offset:" + str(read_offset) + "; size: " + str(read_chunk_size))

        # Crutch
        estimated_size_test = 128*1024 # test 128kb def size
        if 'estimatedSize' not in self.__data:
            print("INVALID TRACK")
            return  # TODO: skip invalid tracks

        if not self.__tag:  # Crating tag only when needed WTAF
            print("#### # # CRATE TAG")
            self.__gen_tag()
            self.__stream_cache += bytearray(self.__rendered_tag)

        tag_length = len(self.__rendered_tag)  # need?

        if read_offset == 0 and not self.__stream_url:
            # print('####### FIRST RUN TRACK')
            self.__stream_url = urllib.request.urlopen(self.__library.get_stream_url(self.id))
            self.__stream_cache += self.__stream_url.read(128 * 1024)  # 128kb?

            pp.pprint(self.__stream_url.__dict__)


        if not self.__stream_url:  # error while getting url
            # TODO:check it
            print("###Could't get stream url!")
            print(self)
            print(self.id)
            print("###")
            #return None

        # If we read the end of the file (the last 4 * 4k) and the track is not half loaded,
        # then we send to the hears of the one who reads
        if read_offset + read_chunk_size >= int(self.__data['estimatedSize'])-5*4096 \
                and len(self.__stream_cache)-tag_length < (int(self.__data['estimatedSize']) / 2
        ):
            print("\033[032moffset:     \t%10.3fK\033[0m" % (read_offset / 1024))
            print("estSize:    \t%10.3fK" % (int(self.__data['estimatedSize'])/1024))
            print("Read Consistently!")
            return None

        # TODO: need test slow connection
        pos = (read_offset + read_chunk_size)

        # Crutch

        print("\n############ debugPos")
        print("tag_length: \t%10.3fK" % (tag_length/1024))
        print("#pos:       \t%10.3fK" % (pos/1024))
        print("offset:     \t%10.3fK" % (read_offset / 1024))
        print("size:       \t%10.3fK" % (read_chunk_size / 1024))
        print("estSize:    \t%10.3fK" % (int(self.__data['estimatedSize'])/1024))

        downloaded_stream_len = len(self.__stream_cache) - tag_length
        diff = downloaded_stream_len - tag_length - (read_offset + read_chunk_size)

        # TODO: move to while.... wtf is this???
        if downloaded_stream_len - read_chunk_size * 2 > read_offset + read_chunk_size:
            print("#from cache...")
        else:
            iter1 = 0
            while True and self.__stream_url:
                print("\033[031m\t\t\tDownloading.. %d chunk\033[0m" % iter1)
                iter1 += 1
                # TODO: check endless loop
                chunk = self.__stream_url.read(read_chunk_size * 2)  # 2 step up

                self.__stream_cache += chunk  # simply offset?#read all now?
                downloaded_stream_len = len(self.__stream_cache) - tag_length
                diff = downloaded_stream_len - tag_length - (read_offset + read_chunk_size)
                print("chunk:      \t%10.3fK" % (len(chunk) / 1024))

                if diff > 0 or len(chunk) == 0:
                    break
            print("\033[031m\t\t\tDownloading..  OK! %d chunks\033[0m" % iter1)

        len_remain = (downloaded_stream_len + tag_length - read_offset - read_chunk_size)
        # Crutch
        print("#remain:    \t%10.3fK" % (len_remain / 1024))
        print("diff:       \t%10.3fK" % (diff / 1024))
        print("#downloaded:\t%10.3fK" % (downloaded_stream_len / 1024))

        if downloaded_stream_len > read_offset:  # TODO: neeed??
            return self.__stream_cache[read_offset:read_offset + read_chunk_size]

        return self.__stream_cache[read_offset:read_offset + read_chunk_size]  # need len tas size????

    def close(self):
        # pass
        """
        if self.__stream_url:
            log.info("#######################################################killing url")
            self.__stream_cache = bytes()

            if self.__rendered_tag:
                self.__stream_cache += bytearray(self.__rendered_tag)

            self.__stream_url.close()
            self.__stream_url = None
        """

    def __str__(self):
        value2 = TRACK_FORMAT.format(self)
        return value2

class Playlist(object):
    """This class manages playlist information"""

    def __init__(self, library, data):
        self.__library = library
        self.__id = data['id']
        self.__name = data['name']
        self.__tracks = {}

        for track in data['tracks']:

            # TODO: WTF IS THIS SHIT?
            track_id = track['trackId']
            try:
                if 'track' in track:
                    album_id = track['track']['albumId']
                    if album_id not in self.__library.albums:
                        self.__library.albums[album_id] = Album(self.__library, track['track'])
                if track_id in self.__library.tracks:
                    tr = self.__library.tracks[track_id]
                else:
                    tr = Track(self.__library, track)

                # FISH
                print('#1debug')
                print(tr)
                print('#1end')
                print(track)

                self.__tracks[tr.title] = tr
            except:
                log.exception("error: {}".format(track))

        log.info("Playlist: {0.name}, {1} tracks".format(self, len(self.__tracks)))

    @property
    def id(self):
        return self.__id

    @property
    def name(self):
        return self.__name

    @property
    def tracks(self):
        return self.__tracks

    def __str__(self):
        return "{0.name}".format(self)


class MusicLibrary(object):
    """This class reads information about your Google Play Music library"""

    def __init__(self, username=None, password=None,
                 true_file_size=False, verbose=0):

        self.verbose = bool(verbose)
        self.api = GoogleMusicAPI(debug_logging=self.verbose)
        self.__login_and_setup(username, password)

        self.__artists = {}
        self.__artists_by_name = {}
        self.__albums = {}
        self.__tracks = {}
        self.__playlists = {}

        self.rescan()

    def __login_and_setup(self, username=None, password=None):
        # If credentials are not specified, get them from $HOME/.gmusicfs
        if not username or not password:
            cred_path = os.path.join(os.path.expanduser('~'), '.gmusicfs/.gmusicfs')
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
            self.config = configparser.ConfigParser()
            self.config.read(cred_path)
            username = self.config.get('credentials', 'username')
            password = self.config.get('credentials', 'password')
            if not username or not password:
                raise NoCredentialException(
                    'No username/password could be read from config file'
                    ': %s' % cred_path)

        log.info('Logging in...')
        self.api.login(username, password, GoogleMusicAPI.FROM_MAC_ADDRESS)
        log.info('Login successful.')

    @property
    def artists(self):
        return self.__artists

    @property
    def artists_by_name(self):
        return self.__artists_by_name

    @property
    def albums(self):
        return self.__albums

    @property
    def playlists(self):
        return self.__playlists

    @property
    def tracks(self):
        return self.__tracks

    def rescan(self):
        """Scan the Google Play Music library"""
        self.__artists = {}
        self.__artists_by_name = {}
        self.__albums = {}
        self.__tracks = {}
        self.__playlists = {}
        self.__populate_library()

    def get_stream_url(self, track_id):
        print("URL:")

        url = self.api.get_stream_url(track_id)
        print(url)
        return url

    def __populate_library(self):
        log.info('Gathering track information...')
        tracks = self.api.get_all_songs()
        errors = 0
        for track in tracks:
            try:
                # make track
                nTrack = Track(self, track)

                print("\n\n\n\n" + str(nTrack))

                if nTrack.id not in self.__tracks:
                    self.__tracks[nTrack.id] = nTrack
                else:
                    print("# ALERT! TRACK ALREADY EXISTS IN TRACK BASE? DUPES?")
                    nTrack = self.__tracks[nTrack.id]

                # make album
                nAlbum = Album(self, track)
                if nAlbum.id not in self.__albums:
                    self.__albums[nAlbum.id] = nAlbum
                    print("# new album in library: " + str(nAlbum))
                else:
                    nAlbum = self.__albums[nAlbum.id]
                    print("# old album from library: " + str(nAlbum))


                # TEST LOGIC. IF TRACK HAVE VALID ALBUM ARTIST, then ARTIST = albumArtist
                if "albumArtist" in track and track["albumArtist"].strip() != "":  # TODO: use track?
                    # make arti
                    nArtist = Artist(self, track, nTrack.album_artist_printable)
                    if nTrack.album_artist_printable not in self.__artists_by_name:
                        self.__artists_by_name[nTrack.album_artist_printable] = nArtist
                        print("# new artist from albumArtist: " + str(nArtist))
                    else:
                        nArtist = self.__artists_by_name[nTrack.album_artist_printable]
                        print("# old artist from albumArtist: " + str(nArtist))
                else:
                    # make artist
                    nArtist = Artist(self, track)
                    if nArtist.name_printable not in self.__artists_by_name:
                        self.__artists_by_name[nArtist.name_printable] = nArtist
                        print("# new artist from artist: " + str(nArtist))
                    else:
                        nArtist = self.__artists_by_name[nArtist.name_printable]
                        print("# old artist from artist: " + str(nArtist))

                nTrack.add_album(nAlbum)
                nTrack.add_artist(nArtist)

                nAlbum.add_track(nTrack)
                nAlbum.add_artist(nArtist)

                nArtist.add_album(nAlbum)

                '''
                print("\n\n\nTRACK:")
                pp.pprint(nTrack.__dict__)
                print("ALBUM:")
                pp.pprint(nAlbum.__dict__)
                print("ARTIST:")
                pp.pprint(nArtist.__dict__)
                '''

            except:
                logging.error(traceback.format_exc())
                log.exception("Error loading track: {}" + str(pp.pprint(track)))
                errors += 1
                raise

        # playlists = self.api.get_all_user_playlist_contents()
        playlists = ""
        for pl in playlists:

            name = format_to_path(pl['name'])

            if name[len(name) - 1] == ".":
                name += "_"
            while name in self.__playlists:
                name += "_"

            if name:
                try:
                    self.__playlists[name] = Playlist(self, pl)
                except:
                    log.exception("Error loading playlist: {}".format(pl))
                    errors += 1

        print("Loaded {} tracks, {} albums, {} artists and {} playlists ({} errors).".format(len(self.__tracks),
                                                                                                len(self.__albums),
                                                                                                len(self.__artists_by_name),
                                                                                                len(self.__playlists),
                                                                                                errors))

    def cleanup(self):
        pass


class GMusicFS(LoggingMixIn, Operations):
    """Google Music Filesystem"""

    def __init__(self, path, username=None, password=None,
                 true_file_size=False, verbose=0, lowercase=True):
        Operations.__init__(self)

        artist = '/artists/(?P<artist>[^/]+)'

        self.artist_dir = re.compile('^{artist}$'.format(artist=artist))

        self.artist_album_dir = re.compile('^{artist}/{album}$'.format(
                artist=artist, album=ALBUM_REGEX
            )
        )
        self.artist_album_track = re.compile('^{artist}/{album}/{track}$'.format(
                artist=artist, album=ALBUM_REGEX, track=TRACK_REGEX
            )
        )

        self.playlist_dir = re.compile('^/playlists/(?P<playlist>[^/]+)$')

        PLAYLIST_REGEX = '^/playlists/(?P<playlist>[^/]+)/' + TRACK_REGEX + '$'
        self.playlist_track = re.compile(PLAYLIST_REGEX)
        print(PLAYLIST_REGEX)

        self.__opened_tracks = {}  # path -> urllib2_obj

        # Login to Google Play Music and parse the tracks:
        self.library = MusicLibrary(username, password, true_file_size=true_file_size, verbose=verbose)
        log.info("Filesystem ready : %s" % path)

    def cleanup(self):
        self.library.cleanup()

    def getattr(self, path, fh=None):
        print(path)
        """Get information about a file or directory"""
        artist_dir_m           = self.artist_dir.match(path)
        artist_album_dir_m     = self.artist_album_dir.match(path)
        artist_album_track_m   = self.artist_album_track.match(path)
        playlist_dir_m         = self.playlist_dir.match(path)
        playlist_track_m       = self.playlist_track.match(path)

        # Default to a directory
        st = {
            'st_mode': (S_IFDIR | 0o755),
            'st_nlink': 2
        }
        date = 0  # Make the date really old, so that cp -u works correctly.
        st['st_ctime'] = st['st_mtime'] = st['st_atime'] = date

        parts = ""
        if path == '/':
            print("path /")
            pass

        elif path == '/artists':
            print("path /artists/")
            pass

        elif path == '/playlists':
            print("path /playlists/")
            pass

        elif artist_dir_m:
            # print("path \"artist_dir_m\"")
            pass

        elif artist_album_dir_m:

            print("path \"artist_album_dir_m\"")

            parts = artist_album_dir_m.groupdict()
            print("parts artist_album_dir_m:")
            print(parts)
            artist = self.library.artists_by_name[parts['artist']]
            print("==================")
            print(artist)
            print("==================")

            if parts['album'] not in artist.albums:
                raise FuseOSError(ENOENT)
                return st

            album = artist.albums[parts['album']]
            st['st_size'] = len(artist.albums)

            print(album)
            print("==================")
            print("############### I ADD DIRECTORY SIZE ")

        elif artist_album_track_m:

            print("path \"artist_album_track_m\"")

            parts = artist_album_track_m.groupdict()
            print("parts artist_album_track_m:")
            print(parts)
            artist = self.library.artists_by_name[parts['artist']]
            album = artist.albums[parts['album']]
            # pp.pprint(album.tracks)
            # pp.pprint(album)
            title2 = parts['disc'] + "-" + parts['number'] + " - " + parts['trartist'] + " - " + parts['title'] + ".mp3"
            track = album.tracks[title2]

            print("t==================")
            print(album)
            print(track)
            print("############### artist_album_track_m I RETURN TRACK ATTR!")
            return track.get_attr()

        elif playlist_dir_m:

            print("path \"playlist_dir_m\"")

            pass
        elif playlist_track_m:

            print("path \"playlist_track_m\"")

            parts = playlist_track_m.groupdict()
            print("parts playlist_track_m:")
            print(parts)
            playlist = self.library.playlists[parts['playlist']]
            # TODO: revert checking
            # if parts['title'] in playlist.tracks:
            if parts['title']:
                print("############### playlist_track_m I RETURN ATTR!")
                track = playlist.tracks[parts['title']]
                # print(playlist.tracks)
                return track.get_attr()
            else:
                print("############### playlist_track_m I RETURN XYU (st)")
                print(playlist.tracks)
                return st
        else:
            print("############### i return FUSE ERROR")
            print(path)
            print(parts)
            raise FuseOSError(ENOENT)

        return st

    def open(self, path, fh):
        log.info("open: {} ({})".format(path, fh))
        artist_album_track_m = self.artist_album_track.match(path)
        playlist_track_m = self.playlist_track.match(path)

        if artist_album_track_m:
            parts = artist_album_track_m.groupdict()
            artist = self.library.artists_by_name[parts['artist']]
            album = artist.albums[parts['album']]
            # TODO: TEST
            title2 = parts['disc'] + "-" + parts['number'] + " - " + parts['trartist'] + " - " + parts['title'] + ".mp3"
            track = album.tracks[title2]
        elif playlist_track_m:
            parts = playlist_track_m.groupdict()
            playlist = self.library.playlists[parts['playlist']]
            track = playlist.tracks[parts['title']]
        else:
            RuntimeError('unexpected opening of path: %r' % path)

        # TODO: check it
        key = path + "-" + str(fh)
        if not fh in self.__opened_tracks:
            self.__opened_tracks[key] = [0, track]

        self.__opened_tracks[key][0] += 1

        return fh

    def release(self, path, fh):
        log.info("release: {} ({})".format(path, fh))
        key = path + "-" + str(fh)
        track = self.__opened_tracks.get(key, None)
        if not track:
            raise RuntimeError('unexpected path: %r' % path)
        track[0] -= 1
        if not track[0]:
            track[1].close()

    def read(self, path, size, offset, fh):
        log.info("read: {} offset: {} size: {} ({})".format(path, offset, size, fh))
        key = path + "-" + str(fh)
        track = self.__opened_tracks.get(key, None)
        if track is None:
            raise RuntimeError('unexpected path: %r' % path)

        return track[1].read(offset, size)

    # TODO: add file sizes... maybe add radio?
    def readdir(self, path, fh):
        artist_dir_m = self.artist_dir.match(path)
        artist_album_dir_m = self.artist_album_dir.match(path)
        playlist_dir_m = self.playlist_dir.match(path)

        if path == '/':
            return ['.', '..', 'artists', 'playlists']

        elif path == '/artists':  # TODO: need filter bad characters?
            listTMP = list(self.library.artists_by_name.keys())
            print(listTMP)  # TODO: remove
            listTMP = ['.', '..'] + listTMP
            return listTMP

        elif path == '/playlists':
            playlistTMP = ['.', '..'] + list(self.library.playlists.keys())
            print(playlistTMP)  # TODO: remove
            return playlistTMP

        elif path == '/radio':
            radioTMP = ['.', '..']  # + list(self.library.radio.keys())
            print(radioTMP)  # TODO: remove
            return radioTMP

        elif artist_dir_m:
            print("artist_dir_m")
            # Artist directory, lists albums.
            parts = artist_dir_m.groupdict()

            print("==========================parts")
            print(parts)

            artist = self.library.artists_by_name[parts['artist']]

            print("========================== artist")
            print(artist)

            print("========================== artist.albums")
            print(artist.albums)

            print("========================== artist.albums.values")
            values = artist.albums.values()
            print(values)
            tmpList = [str(album) for album in artist.albums.values()]
            print(tmpList)
            return ['.', '..'] + tmpList

        elif artist_album_dir_m:
            # Album directory, lists tracks.
            parts = artist_album_dir_m.groupdict()
            artist = self.library.artists_by_name[parts['artist']]
            album = artist.albums[parts['album']]
            return ['.', '..'] + [str(track) for track in album.tracks.values()]

        elif playlist_dir_m:
            # Playlists directory, lists tracks.
            parts = playlist_dir_m.groupdict()
            playlist = self.library.playlists[parts['playlist']]
            return ['.', '..'] + [str(track) for track in playlist.tracks.values()]
        else:
            print("####################################wtf?")
            print("self:")
            print(self)
            print("path:")
            print(path)
            print("fh:")
            print(fh)

        return ['.', '..']


def main():
    log.setLevel(logging.WARNING)
    logging.getLogger('gmusicapi').setLevel(logging.WARNING)
    logging.getLogger('fuse').setLevel(logging.WARNING)
    logging.getLogger('requests.packages.urllib3').setLevel(logging.WARNING)

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
    fs = GMusicFS(mountpoint, true_file_size=args.true_file_size, verbose=verbosity, lowercase=args.lowercase)
    # quit()
    try:
        FUSE(fs, mountpoint, foreground=args.foreground,
             ro=False, nothreads=False, allow_other=args.allow_other, allow_root=args.allow_root, uid=args.uid,
             gid=args.gid)
    finally:
        fs.cleanup()


if __name__ == '__main__':
    main()
