import discord
from discord.ext import commands
from cogs.utils.dataIO import dataIO
from bs4 import BeautifulSoup
from bs4 import Comment
import asyncio
import os
import aiohttp
from urllib.request import quote
import re
import json
from asyncio import Lock
from collections.abc import MutableSequence
from __main__ import send_cmd_help
from cogs import repl


SETTINGS_PATH = "data/pico8/settings.json"
PICKS_PATH =    "data/pico8/picks.json"
ERROR_PATH =    "data/pico8/error.log"
NBS = '​'


class ReactiveList(MutableSequence):
    """calls a callback with the list item when it is accessed
    """

    def __init__(self, *args, callback, **kwargs):
        self.callback = callback
        self._list = list(args[0]) if len(args) else []

    def __getitem__(self, key):
        self.callback(key)
        return self._list[key]

    def __setitem__(self, key, value):
        self._list[key] = value

    def __delitem__(self, key):
        del self._list[key]

    def __len__(self):
        return len(self._list)

    def insert(self, key, value):
        return self._list.insert(key, value)


class BBS:
    """BBS Api Wrapper"""
    BASE = "https://www.lexaloffle.com/bbs/"
    PARAMS = {
        "cat": {
            "VOXATRON":      "6",
            "PICO8":         "7"
        },
        "sub": {
            "DISCUSSIONS":   "1",
            "CARTRIDGES":    "2",
            "WIP":           "3",
            "COLLABORATION": "4",
            "WORKSHOPS":     "5",
            "SUPPORT":       "6",
            "BLOGS":         "7",
            "JAMS":          "8",
            "SNIPPETS":      "9",
            "PIXELS":        "10",
            "ART":           "10",
            "MUSIC":         "11"
        },
        "orderby": {
            "RECENT":        "ts",
            "FEATURED":      "rating",
            "RATING":        "rating",
            "FAVORITES":     "favourites",  # shouldn't be used by bot (yet?)
            "FAVOURITES":    "favourites"
        }
    }
    RE_POSTS = re.compile(r"var pdat=(.*?);\r\n\t\tvar updat", re.DOTALL)
    RE_CART_BG = re.compile("background:url\('(.*?)'\)", re.DOTALL)

    def __init__(self, loop, search, orderby="RECENT", params={}):
        self.url = BBS.BASE
        self.search_term = search
        self.loop = loop
        self.orderby = params.get('orderby', orderby)
        self.params = {}
        for p, v in params.items():
            self.set_param(p, v)
        self.posts = []
        self.current_post = 0
        self.queue = []
        self.embeds = []
        self.load_tasks = ReactiveList(callback=self.queue_area)
        self.locks = []
        self.picks = dataIO.load_json(PICKS_PATH)
        # TODO: later, load them from the site in case of updates?

    def queue_area(self, i):
        self.posts[i]
        self.queue.extend([i, (i + 1) % len(self.posts),
                              (i - 1) % len(self.posts)])

    async def __aenter__(self):
        self.runner = self.loop.create_task(self._queue_runner())
        await self.search(self.search_term, self.orderby)
        return self

    async def __aexit__(self, *args):
        self.runner.cancel()

    def set_search(self, term):
        self.params.update({'search': term})

    async def search(self, term, orderby="RECENT"):
        self.set_search(term)
        self.set_param("orderby", orderby)
        await self._populate_results()
        return self.posts

    async def _populate_results(self):
        raw = await self._get()
        soup = BeautifulSoup(raw, "html.parser")
        async def self_destruct():
            raise RuntimeError("KABOOM!")

        try:
            js_posts = re.search(BBS.RE_POSTS, raw).group(1)
        except AttributeError:  # no results
            self.load_tasks = [
                "No results found", "I said there're no results",
                ":neutral_face:", ":confused:", "what..", 
                "what do you expect to be here?", 
                "I mean it's not like I'd spend time",
                "filling a bunch of flavor text", "for no reason", "...",
                "..right?", "...", "...", "...", "well..", 
                "I guess you could look at some of these..",
                *[(msg, self._post_to_embed(p)) for msg, p in self.picks],
                ("And there's so much more!", 
                 discord.Embed(title='Now go make some of your own!')),
                ("this message will self-destruct in...", discord.Embed(title='3')),
                discord.Embed(title='2'), discord.Embed(title='1'),
                self_destruct()
            ]
            self.load_tasks += self.load_tasks[::-1][:-1]
            self.posts = []
            self.embeds = []
            self.locks = []
            return

        cleanse = [('\r', ''), ('\n', ''), ('\t', ''), ('`', '"'),
                   (',,', ',null,'), (',]', ']')]
        for p, r in cleanse:
            js_posts = js_posts.replace(p, r)

        try:
            posts = json.loads(js_posts)
        except json.decoder.JSONDecodeError as e:
            print('erroring page in ' + ERROR_PATH)
            with open(ERROR_PATH, 'w+') as f:
                f.write(raw)
                f.write('\n\n' + '{:-^50}'.format('scraped'))
                f.write(js_posts)
                f.write('\n\n' + '-'*50)
            self.load_tasks = ['Looks like there was an error :/ '
                               'This is just scraping the forum, '
                               'so there is the possibility this\'ll break']
            return
        # [38386, 28997, `Poop Blaster`,"thumbs/pico38385.png",
        #  0:pid 1:tid 2:title 3:thumb
        # 64,64,"2017-03-18",15018,"chase","2017-03-19",9551,
        # 4:w 5:h 6:date 7:aid 8:author 9:date2 10:uid
        # "kittenm4ster",0,2,0,
        # 11:last 12:likes 13:comments 14:?
        # 7,3,38385,[],0]
        # 15:cat 16:subcat 17:cid 18:tags 19:resolved

        self.posts = [{"PID": p[0],
                       "TID": p[1],
                       "TITLE": p[2],
                       "DESC": None,  # temp
                       "THUMB": self.url + quote('..' + p[3] if p[3][0] == '/' else p[3]),
                       "DATE": p[6],
                       "AID": p[7],
                       "AUTHOR": p[8],
                       "AUTHOR_URL": self.url + "?uid={}".format(p[7]),
                       "AUTHOR_PIC": "https://www.lexaloffle.com/bimg/pi/pi28.png",  # temp
                       "STARS": p[12],
                       "CC": False,  # temp
                       "COMMENTS": p[13],
                       # "FAV": p[14]  # used in generate_cart_preview.
                       # apparently is also the cart id sometimes?
                       "CAT": p[15],
                       "SUB": p[16],
                       "CID": p[17],
                       "PNG": None if ((p[15] not in (6,7)) or p[17] is None) else
                              self.url + ('cposts/{}/{}.p8.png' if p[15] == 7 else
                                          'cposts/{}/cpost{}.png').format(p[17] // 10000, p[17]),
                       "CART_TITLE": None,  # temp
                       "CART_AUTHOR": None,  # temp
                       "TAGS": p[18],
                       "STATUS": "",
                       "URL": "{}?tid={}".format(self.url, p[1]),
                       "PARAM": {"tid": p[1]}} for p in posts]

        for p in self.posts:
            self.embeds.append(None)
            self.locks.append(Lock())

        async def gen_embed(i):
            await self._populate_post(i)
            return self.embeds[i]

        self.load_tasks.extend(gen_embed(i) for i in range(len(self.embeds)))

        await self._populate_post(0)
        self.queue_area(0)

    def _post_to_embed(self, post):
        # this whole embed business should be moved into the Pico8 class
        p = post

        embed = discord.Embed(title=p['TITLE'], url=p['URL'],
                              description=p['DESC'] if p['DESC'].strip() else None)
        embed.set_author(name=p['AUTHOR'], url=p['AUTHOR_URL'],
                         icon_url=p['AUTHOR_PIC'])
        tagline = ("🔖 " if p['TAGS'] else "No tags") + (', '.join(p['TAGS']))
        footer_kw = {}
        if p['CART_TITLE'] is not None:
            embed.set_thumbnail(url=p['PNG'])
            embed.add_field(name=p['CART_TITLE'], inline=True,
                            value='by {}'.format(p['CART_AUTHOR']))
            embed.set_image(url=p['THUMB'])
            cc = self.url[:-5] + "/gfx/set_cc{}.png".format(1 if p['CC'] else 0)
            footer_kw['icon_url'] = cc
        embed.set_footer(text="{} - {} ⭐ - {}".format(p['DATE'], p['STARS'], 
                                                       tagline),
                         **footer_kw)
        return embed

    async def _populate_post(self, index_or_id, post=None):
        # hope this doesn't fail
        index = self._get_post_index(index_or_id)
        post = post or self.posts[index]
        # needlessly complicated
        if post['STATUS'] == 'success':
            return True
        async with self.locks[index]:
            if post['STATUS'] == 'success':
                return True
            post['STATUS'] = 'processing'
            try:
                await self._load_post(index)
            except Exception as e:
                post['STATUS'] = 'failed'
                raise e
            else:
                self.embeds[index] = self._post_to_embed(post)
            post['STATUS'] = 'success'

    async def _load_post(self, index):
        post = self.posts[index]
        raw = await self._get_post(index)
        soup = BeautifulSoup(raw, "html.parser")

        main = soup.find('div', id='p{}'.format(post['PID']))
        # author pic
        ava = main.center.img['src']
        if not ava.startswith('http'):
            ava = self.url[:-5] + quote(ava)
        post['AUTHOR_PIC'] = ava

        cart = soup.find('div', id=re.compile(r'infodiv*'))
        if cart:
            bg = re.search(BBS.RE_CART_BG, cart['style']).group(1)
            # image
            if not post['THUMB'].endswith(bg):
                post['THUMB'] = self.url + quote(bg)
            # thumbnail
            pngel = cart.parent.find_next_sibling()
            png = pngel.a['href']
            if post['PNG'] is None or not post['PNG'].endswith(png):
                post['PNG'] = self.url[:-5] + quote(png)
            # CC
            cc = pngel.find_next_sibling().find_next_sibling()
            post['CC'] = cc.img['src'] == '/gfx/set_cc1.png'
            # cart title / author
            links = cart.find_all('a')
            post['CART_TITLE'] = links[0].text
            post['CART_AUTHOR'] = links[1].text

        # description
        # try remove the cart(s)
        try:
            # first one we don't want to see
            cart.parent.parent.extract()
        except AttributeError:
            pass
        # other carts we might want info on title
        for ct in soup.find_all('div', id=re.compile(r'infodiv*')):
            ctitle = ct.find_all('a')[0].text
            ct.parent.parent.insert_after(soup.new_string('[{}]'.format(ctitle)))
            ct.parent.parent.extract()

        # remove all scripts, styles, comments
        for sc in main(['script', 'style']):
            sc.extract()
        for cmt in main.find_all(string=lambda text: isinstance(text, Comment)):
            cmt.extract()

        # replace brs with 2 no-break spaces to be replaced later
        for br in main.find_all('br'):
            br.replace_with(soup.new_string(NBS + NBS))

        ps = [p.text.strip() for p in main.find_all('p')]

        # clean out unwanted \r \n and put brs back in as \n
        cleanse = (('\r', ''), ('\n', ''), (NBS + NBS, '\n'))
        for i in range(len(ps)):
            for p, r in cleanse:
                ps[i] = ps[i].replace(p, r)

        post['DESC'] = '\n\n'.join(ps)[:150]
        if post['DESC'].strip():
            post['DESC'] += '...'

    def _get_post_index(self, index_or_id):
        try:
            self.posts[index_or_id]
        except TypeError:
            for n, p in enumerate(self.posts):
                if p["PARAM"]['tid'] == index_or_id:
                    return n
        else:
            return index_or_id
        raise KeyError('index does not exist in posts')

    async def _get_post(self, index_or_id):
        index = self._get_post_index(index_or_id)
        post = self.posts[index]
        param = post["PARAM"]
        return await self._get(param)

    async def _get(self, params=None):
        params = params or self.params
        async with aiohttp.get(self.url, params=params) as r:
            return await r.text()

    async def _queue_runner(self):
        while True:
            if self.queue:
                working_group = []
                for i in self.queue[:]:
                    status = self.posts[i]["STATUS"]
                    if status == 'success':
                        self.queue.remove(i)
                    if status in ('', 'failed'):
                        working_group.append(i)
                for i in working_group:
                    self.loop.create_task(self._populate_post(i))
            await asyncio.sleep(.5)

    def set_param(self, param, value_name):
        self.params[param] = self.get_value(param, value_name)

    def set_param_by_prefix(self, param, prefix):
        value_name = self.get_value_name_by_prefix(param, prefix)
        return self.add_param(param, value_name)

    def param_exists(self, param):
        return param in BBS.PARAMS

    def value_name_exists(self, param, value_name):
        return self.param_exists(param) and value_name in BBS.PARAMS[param]

    def get_value(self, param, value_name):
        return BBS.PARAMS[param][value_name]

    def get_value_by_prefix(self, param, prefix):
        value_name = self.get_value_name_by_prefix(param, prefix)
        return self.get_value(param, value_name)

    def get_value_name_by_prefix(self, param, prefix):
        group = BBS.PARAMS[param]
        upper_no_s = prefix.upper()[-1]

        for name in group:
            if name.startswith(upper_no_s):
                return name

        raise ValueError('Prefix {} not found in param {}'
                         .format(prefix, param))

    def add_to_queue(self, post):
        if post in self.queue:
            return False
        self.queue.append(post)

# [{'href':t.a['href'], 'text':t.a.text}  for t in s.find_all(id=re.compile("pdat_.*"))]

"""
UI Ideas:

Cart: 
    png for thumbnail
    in code:
        -- cart name
        -- by author

Other:
    link
    author thumbnail somewhere
    author name
    title
    description[:n_chars] + '...'
    stars
    hearts
    CC?
    tags
    date


Card:
    [Title](link to post)    author.avatar
    Description

    Cart Name           Tags:
    by author           tags, tags, tags

    Cart thumbnail (or png cart?) or default "no cart" logo

    footer: date, CC, hearts, starts

    controls:
    < x >

"""


class Pico8:
    """cog to search Lexaloffle's bulletin board system
    and notify when new PICO-8 carts are uploaded

    [p]bbs [filters=?p8:recent] [search_terms] uses its own filter format.

    filters must start with a question mark(?)
    or else they will be treated as search terms

    filters must be separated by a colon(:) 
    but don't have to be in a particular order

    If any part of a filter is left blank, the defaults will be used,
    for example, `[p]bbs` will search for all recent pico8 carts

    Use [p]help bbs for the list of filters available"""

    def __init__(self, bot):
        self.bot = bot
        self.settings = dataIO.load_json(SETTINGS_PATH)
        self.searches = []

    @commands.command(pass_context=True, no_pm=True, aliases=['pico8'])
    async def bbs(self, ctx, *, filters="?p8:recent", search_terms=""):
        """Search PICO-8's bbs with an optional filter
        
        use [p]help Pico8 for more info about filters

        Filters Available
          System:
              pico8 (p8) [default]
              voxatron (vox)
          Category:
              [none] [default]
              jams     snippets           discussions (discuss)
              blogs    inprogress (wip)   collaboration (collab)
              arts     support            workshops (wkshop/shop)
              music    cartridges (carts)
          Order:
              new [default]
              rating
        """
        author = ctx.message.author
        server = ctx.message.server
        msg = ctx.message

        choices = {
            "cat": {
                "pico8": "PICO8", "p8": "PICO8",
                "voxatron": "VOXATRON", "vox": "VOXATRON",
            },
            "sub": {
                "jams": "JAMS",
                "snippets": "SNIPPETS",
                "discussions": "DISCUSSIONS", "discuss": "DISCUSSIONS",
                "blogs": "BLOGS",
                "inprogress": "WIP", "wip": "WIP",
                "collaboration": "COLLABORATION", "collab": "COLLABORATION",
                "arts": "ART",
                "support": "SUPPORT",
                "workshops": "WORKSHOPS", "wkshop": "WORKSHOPS",
                "shop": "WORKSHOPS",
                "music": "MUSIC",
                "cartridges": "CARTRIDGES", "carts": "CARTRIDGES"
            },
            "orderby": {
                "new": "RECENT",
                "rating": "FEATURED"
            }
        }

        params = {}

        if filters.startswith('?'):
            filters = filters if ' ' in filters else filters + ' '
            filters, search_terms = filters.split(' ', 1)
            filters = filters[1:].lower().split(':')
            for f in filters:
                for k, ch in choices.items():
                    if f in ch:
                        if k in params:
                            return await self.bot.say("You can only have one"
                                                      "of each filter type.")
                        params[k] = ch[f]
        else:
            search_terms = filters

        await self.bot.add_reaction(msg, '🔎')

        async with BBS(self.bot.loop, search_terms, params=params) as bbs:
            # self.searches.append(bbs)  # add caching later?
            await asyncio.gather(
                repl.interactive_results(self.bot, ctx, bbs.load_tasks, 
                                         timeout=60 * 5),
                self.bot.remove_reaction(msg, '🔎', server.me)
            )


def check_folders():
    paths = ("data/pico8", )
    for path in paths:
        if not os.path.exists(path):
            print("Creating {} folder...".format(path))
            os.makedirs(path)


def check_files():
    default = {}

    if not dataIO.is_valid_json(SETTINGS_PATH):
        print("Creating default pico8 settings.json...")
        dataIO.save_json(SETTINGS_PATH, default)
    else:  # consistency check
        current = dataIO.load_json(SETTINGS_PATH)
        if current.keys() != default.keys():
            for key in default.keys():
                if key not in current.keys():
                    current[key] = default[key]
                    print("Adding " + str(key) +
                          " field to pico8 settings.json")
            dataIO.save_json(SETTINGS_PATH, current)


def setup(bot):
    check_folders()
    check_files()
    n = Pico8(bot)
    bot.add_cog(n)
