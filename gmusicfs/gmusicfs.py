#!/usr/bin/env python2

import os
import re
import sys
import urllib
import configparser as ConfigParser
from errno import ENOENT
from stat import S_IFDIR, S_IFREG
import argparse
import tempfile
import logging
import pprint

from eyed3.id3 import Tag, ID3_V2_4
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn#, fuse_get_context
#import gmusicapi.exceptions
from gmusicapi import Mobileclient as GoogleMusicAPI
#from gmusicapi import Webclient as GoogleMusicWebAPI

#reload(sys)  # Reload does the trick
#sys.setdefaultencoding('UTF-8')

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger('gmusicfs')
pp = pprint.PrettyPrinter(indent=4)  # For debug logging

ALBUM_REGEX = '(?P<album>[^/]+) \((?P<year>[0-9]{4})\)'
ALBUM_FORMAT = u'{name} ({year:04d})'

TRACK_REGEX = '(?P<track>(?P<number>[0-9]+) - (?P<title>.*)\.mp3)'
TRACK_FORMAT = '{number:02d} - {name}.mp3'

ID3V1_TRAILER_SIZE = 128

def formatNames(string_from):
    """Format a name to make it suitable to use as a filename"""
    return re.sub('/', '-', string_from)


class NoCredentialException(Exception):
    pass


class Artist(object):

    def __init__(self, library, data):
        self.__library = library
        self.__id = data['artistId'][0]
        self.__name = data['artist']
        self.__albums = {}

    @property
    def id(self):
        return self.__id

    @property
    def name(self):
        return self.__name

    @property
    def albums(self):
        return self.__albums

    def add_album(self, album):
        self.__albums[album.title] = album

    def __str__(self):
        return "{0.name}".format(self)

class Album(object):

    def __init__(self, library, data):
        self.__library = library
        self.__id = data['albumId']
        self.__artist = self.__library.artists.get(data['artistId'][0], None)
        self.__title = data['album']
        self.__tracks = {}
        self.__year = 0
        if 'albumArtRef' in data:
            self.__art_url = data['albumArtRef'][0]['url']
        else:
            self.__art_url = None
        self.__art = None
        self.__album_info = None

    @property
    def id(self):
        return self.__id

    @property
    def tracks(self):
        if not self.__album_info: # Load all the tracks only on request
            try:
                self.__album_info = self.__library.api.get_album_info(self.__id)
                for track in self.__album_info['tracks']:
                    self.add_track(Track(self.__library, track))
            except:
                log.exception("Error loading album info")
        return self.__tracks

    @property
    def title(self):
        return self.__title

    @property
    def year(self):
        if not self.__year:
            self.__get_year()
        return self.__year

    @property
    def artist(self):
        return self.__artist

    @property
    def art(self):
        if not self.__art:
            self.__load_art()
        return self.__art

    def add_track(self, track):
        self.__tracks[track.title] = track
        if track.id not in self.__library.tracks:
            self.__library.tracks[track.id] = track

    def __get_year(self):
        # some tracks are not loaded from album_info, let's use them to get the album date release
        for track in self.__tracks.values():
            self.__year = track.year or self.__year

    #TODO: need check image type! custom tracks has png cover
    def __load_art(self):
        if not self.__art_url:
            return
        log.info("loading art album: {0.title}".format(self))
        self.__art = bytes()
        u = urllib.request.urlopen(self.__art_url)
        #TODO: wtf?
        data = u.read()
        while data:
            self.__art += data
            data = u.read()

        #TODO: caching
        f = open("/home/fish/img", "wb")
        f.write(self.__art)      # str() converts to string
        f.close()

        log.info("loading art album: {0.title}".format(self)+" done!")

    def __str__(self):
        return "{0.title} ({0.year:04d})".format(self)

class Track(object):

    def __init__(self, library, data):

        errorFlag = 0
        #FISH
        if data['kind'] != 'sj#track':
            print ("\n---------------------------\nnot a track?")
        else:
            '''
            print ("\n\n\n########################\n" + data['id'] + " - sj#track!\n")
            print (data)
            print ("########################")
            '''

        self.__library = library
        if 'track' in data: # Playlists manage tracks in a different way
            self.__id = data['trackId']
            data = data['track']
        elif 'id' in data:
            self.__id = data['id']
        elif 'storeId' in data:
            self.__id = data['storeId']
        else:
            self.__id = data['nid']

        self.__data = data

        #FISH

        #ex bad data
        '''
        {
            'kind': 'sj#playlistEntry',
            'id': '147b7887-2f6a-38b0-9d0e-4168fa881aed',
            'clientId': 'e3159570-6473-4ee0-b437-5ee7d2c73650',
            'playlistId': '558a40fe-4e64-434f-b90f-72185bae5e66',
            'absolutePosition': '01503059504007927071',
            'trackId': '6e8d33df-b04c-3b99-b6b3-dc38cb40f609',
            'creationTimestamp': '1481614791376812',
            'lastModifiedTimestamp': '1481614791376812',
            'deleted': False,
            'source': '1'
        }

        ########################
        2f902400-5eb9-3ae5-bc7b-c554664e9c1c - #NOT A TRACK!

        {
            'kind': 'sj#playlistEntry',
            'id': '2f902400-5eb9-3ae5-bc7b-c554664e9c1c',
            'clientId': '821a5d33-67dd-4b69-b706-add338c44167',
            'playlistId': 'd6303ffa-5c5b-4b0a-b38f-54fe1e755f02',
            'absolutePosition': '02017612633061982205',
            'trackId': 'Tgarv4vwobsxyxtsott6p4qok5q',
            'creationTimestamp': '1491432379665824',
            'lastModifiedTimestamp': '1491432379665824',
            'deleted': False,
            'source': '2',
            'track': {
                'kind': 'sj#track',
                'title': 'Creep',
                'artist': 'Radiohead',
                'composer': '',
                'album': 'Pablo Honey',
                'albumArtist': 'Radiohead',
                'year': 1993,
                'trackNumber': 2,
                'genre': "'90s Alternative",
                'durationMillis': '238000',
                'albumArtRef': [
                    {
                        'kind': 'sj#imageRef',
                        'url': 'http://lh3.googleusercontent.com/S7EzegIDPI54OwPL9-KNd4hIn2jDZOosfMOh6gVpyEBs397rk33UjqZh9s9IhFlbcROF3CtC',
                        'aspectRatio': '1',
                        'autogen': False
                    }
                ],
                 'artistArtRef': [
                    {
                        'kind': 'sj#imageRef',
                        'url': 'http://lh3.googleusercontent.com/53cYhGcuBl6tJh4NAsrkxHW2dYReUv27bwrA1nb_KNCrgIKeGjhfl-NmUzsu6mJGoyg1UBuvpDM',
                        'aspectRatio': '2',
                        'autogen': False
                    }
                ],
                'playCount': 11,
                'discNumber': 1,
                'rating': '5',
                'estimatedSize': '9548304',
                'trackType': '7',
                'storeId': 'Tgarv4vwobsxyxtsott6p4qok5q',
                'albumId': 'Bgpaifizbaqexiz7gbmlga35c6a',
                'artistId': ['
                    A3qpbllyfot4yhqo7isoomtctli'
                ],
                'nid': 'garv4vwobsxyxtsott6p4qok5q',
                'trackAvailableForSubscription': True,
                'trackAvailableForPurchase': True,
                'albumAvailableForPurchase': False,
                'explicitType': '1',
                'lastRatingChangeTimestamp': '1483988691991000'
            }
        }
        ########################


        #ex good data
        {
            'kind': 'sj#track',
            'id': '9f759b7e-8aab-30d9-ab48-67bdfc7bd00d',
            'clientId': '8ed67819-1bc3-4f9b-aff6-d407a2b91b7c',
            'creationTimestamp': '1493610464864279',
            'lastModifiedTimestamp': '1495531845157253',
            'recentTimestamp': '1493610464842000',
            'deleted': False,
            'title': 'Rule the World',
            'artist': 'Kamelot',
            'composer': '',
            'album': 'Ghost Opera: The Second Coming',
            'albumArtist': 'Kamelot',
            'year': 2007,
            'trackNumber': 2,
            'genre': 'Metal',
            'durationMillis': '220000',
            'albumArtRef': [
                {
                    'kind': 'sj#imageRef',
                    'url': 'http://lh3.googleusercontent.com/fBMethQQloWCqOkoIWGZNRX_b-B_hhuI6U4mEoxhC930U7JBGCEkaNvmTaHkplKLbJuei_XEPg',
                    'aspectRatio': '1',
                    'autogen': False
                }
            ],
            'artistArtRef': [
                {
                    'kind': 'sj#imageRef',
                    'url': 'http://lh3.googleusercontent.com/5v7rwEcVskiwIbcubGbIRsZjzlv7PC8ArTjrVForJx4KbGX2JEfvIuY7WfyHpojcxlEH6KshjLA',
                    'aspectRatio': '2',
                    'autogen': False
                }
            ],
            'playCount': 6,
            'discNumber': 1,
            'estimatedSize': '8832549',
            'trackType': '8',
            'storeId': 'Tqjbcpweva45farkyrlnm6vrrl4',
            'albumId': 'Bglg6zsrdbcqb4t3omqtyhcznb4',
            'artistId': [
                'A7hirrnzyb5mdmdrd2avu7zmzzy'
            ],
            'nid': 'Tqjbcpweva45farkyrlnm6vrrl4',
            'explicitType': '2'
        }
        #end data
        '''
        try:
            self.__title = data['title']
        except Exception:
            errorFlag = 1
            self.__title = ""
            print ("error in __title")

        try:
            self.__number = int(data['trackNumber'])
        except Exception:
            errorFlag = 1
            self.__number = 0;
            print ("error in __number")



        try:
            self.__year = int(data.get('year', 0))
        except Exception:
            errorFlag = 1
            self.__year = 0
            print ("error in __year")




        try:
            self.__album = self.__library.albums.get(data['albumId'], None)
        except Exception:
            errorFlag = 1
            self.__album = ""
            print ("error in __album")

        if errorFlag == 1:
            print ("########################\n" + data['id'] + " - #NOT A TRACK!\n")
            print (data)
            print ("########################\n---------------------------------------")


        self.__url = None

        self.__stream_cache = bytes()
        self.__rendered_tag = bytes()
        self.__tag = None

    def __gen_tag(self):
        log.info("Creating tag idv3...")
        self.__tag = Tag()
        self.__tag.album = self.__data['album']
        self.__tag.artist = self.__data['artist']

        if 'album' in self.__data:
            self.__tag.album = self.__data['album']
        if 'artist' in self.__data:
            self.__tag.artist = self.__data['artist']
        if 'title' in self.__data:
            self.__tag.title = self.__data['title']
        if 'discNumber' in self.__data:
            self.__tag.disc_num = int(self.__data['discNumber'])
        if 'trackNumber' in self.__data:
            self.__tag.track_num = int(self.__data['trackNumber'])
        if 'genre' in self.__data:
            self.__tag.genre = self.__data['genre']
        if 'albumArtist' in self.__data       and self.__data['albumArtist'] != 'genre' in self.__data:
            self.__tag.album_artist = self.__data['albumArtist']
        if 'year' in self.__data and int(self.__data['year']) != 0:
            self.__tag.recording_date = self.__data['year']

        if self.album and self.album.art:
            self.__tag.images.set(0x03, self.album.art, 'image/jpeg', u'Front cover')

        tmpfd, tmpfile = tempfile.mkstemp()
        os.close(tmpfd)
        self.__tag.save(tmpfile, ID3_V2_4)
        tmpfd = open(tmpfile, "rb")
        self.__rendered_tag = tmpfd.read()
        tmpfd.close()
        os.unlink(tmpfile)

    @property
    def id(self):
        return self.__id

    #FISH
    @property
    def number(self):

        try:
            numberT = self.__number
            return numberT
        except Exception:
            print ("no track number")
            return 0


    @property
    def title(self):
        return self.__title

    @property
    def album(self):
        return self.__album

    @property
    def year(self):
        return self.__year

    def get_attr(self):
        print(self.__data)
        #if self.__data['kind'] != "sj#track":


        st = {}
        st['st_mode'] = (S_IFREG | 0o444)
        st['st_nlink'] = 1
        #todo: need good data
        st['st_ctime'] = st['st_mtime'] = st['st_atime'] = 0

        if 'bytes' in self.__data:
            st['st_size'] = int(self.__data['bytes'])
        elif 'estimatedSize' in self.__data:
            st['st_size'] = int(self.__data['estimatedSize'])
        else:
            #fish
            if 'tagSize' in self.__data:
                st['st_size'] = int(self.__data['tagSize']) #wtf??
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
        #pass
        self.__url = urllib.request.urlopen(self.__library.get_stream_url(self.id))
        self.__stream_cache += self.__url.read(32*1024) # Some caching

    def read(self, offset, size):
        if not self.__tag: # Crating tag only when needed
            self.__gen_tag()
            varTag = bytearray(self.__rendered_tag)
            self.__stream_cache+= varTag

        if offset == 0 and not self.__url:
            self.__url = urllib.request.urlopen(self.__library.get_stream_url(self.id))


        if not self.__url:
            return ''

        self.__stream_cache += self.__url.read(offset + size - len(self.__stream_cache))

        return self.__stream_cache[offset:offset + size]

    def close(self):
        pass
        #if self.__url:
        #    log.info("killing url")
        #    self.__stream_cache = str(self.__rendered_tag or "")
        #    self.__url.close()
        #    self.__url = None

    def __str__(self):
        return "{0.number:02d} - {0.title}.mp3".format(self)

class Playlist(object):
    """This class manages playlist information"""

    def __init__(self, library, data):
        self.__library = library
        self.__id = data['id']
        self.__name = data['name']
        self.__tracks = {}


        for track in data['tracks']:
            trackId = track['trackId']
            try:
                if 'track' in track:
                    albumId = track['track']['albumId']
                    if albumId not in self.__library.albums:
                        self.__library.albums[albumId] = Album(self.__library, track['track'])
                if trackId in self.__library.tracks:
                    tr = self.__library.tracks[trackId]
                else:
                    tr = Track(self.__library, track)
                #FISH
                print ('#1debug')
                print (tr)
                print ('#1end')
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
        self.rescan()

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

    def get_stream_url(self, trackId):
        url = self.api.get_stream_url(trackId)
        return url

    def __populate_library(self):
        log.info('Gathering track information...')
        tracks = self.api.get_all_songs()
        errors = 0
        for track in tracks:
            try:
                log.debug('track = %s' % pp.pformat(track))

                if 'artistId' not in track:
                    track['artistId'] = track['artist'] # if we don't have an artistID, use the name as the id

                if not track['artistId']:
                    print ("#artistID is empty")
                    continue
                else:
                    artistId = track['artistId'][0]
                    '''
                    print ("#__populate_library_1")
                    print ("\ttrack['artistId'][0]:")
                    print (track['artistId'])
                    print ("#end __populate_library_1")
                    '''

                if artistId not in self.__artists:
                    self.__artists[artistId] = Artist(self, track)
                    self.__artists_by_name[str(self.__artists[artistId])] = self.__artists[artistId]
                artist = self.__artists[artistId]

                if 'albumId' not in track:
                    track['albumId'] = track['title']

                albumId = track['albumId']
                if albumId not in self.__albums:
                    self.__albums[albumId] = Album(self, track)
                    artist.add_album(self.__albums[albumId])
                album = self.__albums[albumId]

                track = Track(self, track)
                if track.id not in self.__tracks:
                    self.__tracks[track.id] = track
                    album.add_track(track)
            except:
                log.exception("Error loading track: {}".format(track))
                errors += 1

        playlists = self.api.get_all_user_playlist_contents()
        for pl in playlists:
            if pl['name']:
                try:
                    self.__playlists[pl['name']] = Playlist(self, pl)
                except:
                    log.exception("Error loading playlist: {}".format(pl))
                    errors += 1

        log.info("Loaded {} tracks, {} albums, {} artists and {} playlists ({} errors).".format(len(self.__tracks), len(self.__albums), len(self.__artists), len(self.__playlists), errors))

    def cleanup(self):
        pass

class GMusicFS(LoggingMixIn, Operations):
    """Google Music Filesystem"""

    def __init__(self, path, username=None, password=None,
                 true_file_size=False, verbose=0, lowercase=True):
        Operations.__init__(self)

        artist = '/artists/(?P<artist>[^/]+)'

        self.artist_dir = re.compile('^{artist}$'.format(
            artist=artist))
        self.artist_album_dir = re.compile('^{artist}/{album}$'.format(
            artist=artist, album=ALBUM_REGEX))
        self.artist_album_track = re.compile('^{artist}/{album}/{track}$'.format(
            artist=artist, album=ALBUM_REGEX, track=TRACK_REGEX))

        self.playlist_dir = re.compile('^/playlists/(?P<playlist>[^/]+)$')
        #self.playlist_track = re.compile('^/playlists/(?P<playlist>[^/]+)/(?P<track>[^/]+\.mp3)$')
        self.playlist_track = re.compile('^/playlists/(?P<playlist>[^/]+)/(?P<track>(?P<number>[0-9]+) - (?P<title>.*)\.mp3)$')

        self.__opened_tracks = {}  # path -> urllib2_obj

        # Login to Google Play Music and parse the tracks:
        self.library = MusicLibrary(username, password,
                                    true_file_size=true_file_size, verbose=verbose)
        log.info("Filesystem ready : %s" % path)

    def cleanup(self):
        self.library.cleanup()

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
            artist = self.library.artists_by_name[parts['artist']]
            album = artist.albums[parts['album']]
            st['st_size'] = len(artist.albums)

        elif artist_album_track_m:
            parts = artist_album_track_m.groupdict()
            artist = self.library.artists_by_name[parts['artist']]
            album = artist.albums[parts['album']]
            track = album.tracks[parts['title']]
            return track.get_attr()

        elif playlist_dir_m:
            pass

        elif playlist_track_m:
            parts = playlist_track_m.groupdict()
            playlist = self.library.playlists[parts['playlist']]
            if parts['title'] in playlist.tracks:
                track = playlist.tracks[parts['title']]
                return track.get_attr()
            else:
                return st
        else:
            raise FuseOSError(ENOENT)

        return st

    def open(self, path, fh):
        #log.info("open: {} ({})".format(path, fh))
        artist_album_track_m = self.artist_album_track.match(path)
        playlist_track_m = self.playlist_track.match(path)

        if artist_album_track_m:
            parts = artist_album_track_m.groupdict()
            artist = self.library.artists_by_name[parts['artist']]
            album = artist.albums[parts['album']]
            track = album.tracks[parts['title']]
        elif playlist_track_m:
            parts = playlist_track_m.groupdict()
            playlist = self.library.playlists[parts['playlist']]
            track = playlist.tracks[parts['title']]
        else:
            RuntimeError('unexpected opening of path: %r' % path)

        key = path + "-" + str(fh)
        if not fh in self.__opened_tracks:
            self.__opened_tracks[key] = [0, track]

        self.__opened_tracks[key][0] += 1

        return fh

    def release(self, path, fh):
        #log.info("release: {} ({})".format(path, fh))
        key = path + "-" + str(fh)
        track = self.__opened_tracks.get(key, None)
        if not track:
            raise RuntimeError('unexpected path: %r' % path)
        track[0] -= 1
        if not track[0]:
            track[1].close()

    def read(self, path, size, offset, fh):
        #log.info("read: {} offset: {} size: {} ({})".format(path, offset, size, fh))
        key = path + "-" + str(fh)
        track = self.__opened_tracks.get(key, None)
        if track is None:
            raise RuntimeError('unexpected path: %r' % path)

        return track[1].read(offset, size)


    #TODO: need... smth... make a wrapper
    def readdir(self, path, fh):
        artist_dir_m = self.artist_dir.match(path)
        artist_album_dir_m = self.artist_album_dir.match(path)
        playlist_dir_m = self.playlist_dir.match(path)

        if path == '/':
            return ['.', '..', 'artists', 'playlists']

        elif path == '/artists':#TODO: neeed filter bad characters
            listTMP = list(self.library.artists_by_name.keys())

            for index, item in enumerate(listTMP):
                tmpVar2 = str(item.encode('ascii', 'ignore').decode())#pease of shit
                listTMP[index] = tmpVar2

            try:
                listTMP.remove('')
            except Exception:
                print("not has ''")



            listTMP.sort()
            listTMP = set(listTMP)
            print(listTMP)
            return  ['.','..'] + listTMP

        elif path == '/playlists':
            return  ['.','..'] + list(self.library.playlists.keys())

        elif artist_dir_m:
            # Artist directory, lists albums.
            parts = artist_dir_m.groupdict()
            artist = self.library.artists_by_name[parts['artist']]
            return ['.', '..'] + [str(album) for album in artist.albums.values()]

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
    try:
        FUSE(fs, mountpoint, foreground=args.foreground,
                    ro=True, nothreads=True, allow_other=args.allow_other, allow_root=args.allow_root, uid=args.uid, gid=args.gid)
    finally:
        fs.cleanup()

if __name__ == '__main__':
    main()
