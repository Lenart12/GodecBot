import asyncio
import itertools
import random

import discord
from async_timeout import timeout
from discord.ext import commands
import httpx
from bs4 import BeautifulSoup

import ytdl

class VoiceError(Exception):
    pass

class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: ytdl.YTDLSource):
        self.source = source
        self.requester = source.requester
    
    def create_embed(self):
        embed = (discord.Embed(title='Now playing', description='```css\n{0.source.title}\n```'.format(self), color=discord.Color.blurple())
                .add_field(name='Duration', value=self.source.duration)
                .add_field(name='Requested by', value=self.requester.mention)
                .add_field(name='Uploader', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                .add_field(name='URL', value='[Click]({0.source.url})'.format(self))
                .set_thumbnail(url=self.source.thumbnail)
                .set_author(name=self.requester.name, icon_url=self.requester.avatar_url))
        return embed


class SongQueue(asyncio.Queue):    
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()
        self.song_history = []
        self.exists = True

        self._loop = False
        self._autoplay = True
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def autoplay(self):
        return self._autoplay

    @autoplay.setter
    def autoplay(self, value: bool):
        self._autoplay = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()
            self.now = None

            if self.loop == False:
                # If autoplay is turned on wait 3 seconds for a new song.
                # If no song is found find a new one,
                # else if autoplay is turned off try to get the
                # next song within 3 minutes.
                # If no song will be added to the queue in time,
                # the player will disconnect due to performance
                # reasons.
                if self.autoplay and self.current:
                    try:
                        async with timeout(3): 
                            self.current = await self.songs.get()
                    except asyncio.TimeoutError:
                        # Spoof user agent to show whole page.
                        headers = {'User-Agent' : 'Mozilla/5.0 (compatible; Bingbot/2.0; +http://www.bing.com/bingbot.htm)'}
                        song_url = self.current.source.url
                        # Get the page
                        async with httpx.AsyncClient() as client:
                            response = await client.get(song_url, headers=headers)

                        soup = BeautifulSoup(response.text, features='lxml')

                        # Parse all the recommended videos out of the response and store them in a list
                        recommended_urls = []
                        for li in soup.find_all('li', class_='related-list-item'):
                            a = li.find('a')

                            # Only videos (no mixes or playlists)
                            if 'content-link' in a.attrs['class']:
                                recommended_urls.append(f'https://www.youtube.com{a.get("href")}')

                        ctx = self._ctx

                        # Chose the next song so that it wasnt played recently

                        next_song = recommended_urls[0]

                        for recommended_url in recommended_urls:
                            not_in_history = True
                            for song in self.song_history[:15]:
                                if recommended_url == song.source.url:
                                    not_in_history = False
                                    break
                            
                            if not_in_history:
                                next_song = recommended_url
                                break

                        async with ctx.typing():
                            try:
                                source = await ytdl.YTDLSource.create_source(ctx, next_song, loop=self.bot.loop)
                            except ytdl.YTDLError as e:
                                await ctx.send('An error occurred while processing this request: {}'.format(str(e)))
                                self.bot.loop.create_task(self.stop())
                                self.exists = False
                                return
                            else:
                                song = Song(source)
                                self.current = song
                                await ctx.send('Autoplaying {}'.format(str(source)))
                        
                else:
                    try:
                        async with timeout(180):  # 3 minutes
                            self.current = await self.songs.get()
                    except asyncio.TimeoutError:
                        self.bot.loop.create_task(self.stop())
                        self.exists = False
                        return
                
                self.song_history.insert(0, self.current)
                self.current.source.volume = self._volume
                self.voice.play(self.current.source, after=self.play_next_song)
                await self.current.source.channel.send(embed=self.current.create_embed())
            
            #If the song is looped
            elif self.loop == True:
                self.song_history.insert(0, self.current)
                self.now = discord.FFmpegPCMAudio(self.current.source.stream_url, **ytdl.YTDLSource.FFMPEG_OPTIONS)
                self.voice.play(self.now, after=self.play_next_song)
            
            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))
        
        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None
