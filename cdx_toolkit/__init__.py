import requests
import logging
import re
import time
import datetime
import json

LOGGER = logging.getLogger(__name__)


def myrequests_get(url, params=None):
    retry = True
    while retry:
        try:
            resp = requests.get(url, params=params)
            if resp.status_code == 400 and 'page' not in params:
                raise RuntimeError('invalid url of some sort: '+url)
            if resp.status_code in (400, 404):
                LOGGER.debug('giving up with status %d', resp.status_code)
                # 400: html error page -- probably page= is too big
                # 404: {'error': 'No Captures found for: www.pbxxxxxxm.com/*'} -- not an error
                retry = False
                break
            if resp.status_code in (503, 502, 504):  # 503=slow down, 50[24] are temporary outages
                LOGGER.debug('retrying after 1s for %d', resp.status_code)
                time.sleep(1)
                continue
            resp.raise_for_status()
            retry = False
        except ConnectionError:
            LOGGER.warning('retrying after 1s for ConnectionError')
            time.sleep(1)
        except requests.exceptions.RequestException as e:
            LOGGER.warning('something unexpected happened, giving up after %s', str(e))
            raise
    return resp


def get_cc_endpoints(cc_duration, cc_sort):
    # TODO: cache me
    r = myrequests_get('http://index.commoncrawl.org/collinfo.json')
    if r.status_code != 200:
        raise RuntimeError('error getting list of common crawl indices: '+str(r.status_code))

    j = r.json()
    endpoints = [x['cdx-api'] for x in j]
    if len(endpoints) < 30:  # last seen to be 39
        raise ValueError('Surprisingly few endoints for common crawl index')

    # sort newest to oldest to apply the cc_duration
    endpoints = sorted(endpoints, reverse=True)

    ret = []
    if cc_duration.endswith('d') and cc_duration[:-1].isdigit():
        days = int(cc_duration[:-1])
        TIMESTAMP_8 = '%Y%m%d'
        startdate = datetime.datetime.fromtimestamp(time.time()) - datetime.timedelta(days=days)
        startdate = startdate.strftime(TIMESTAMP_8)

        timestamps = re.findall(r'CC-MAIN-(\d\d\d\d-\d\d)', ''.join(endpoints))
        # I think these are ISO weeks
        CC_TIMESTAMP = '%Y-%W-%w'
        for timestamp in timestamps:
            thisdate = datetime.datetime.strptime(timestamp+'-0', CC_TIMESTAMP).strftime(TIMESTAMP_8)
            if thisdate > startdate:
                ret.append(timestamp)
        ret = ['http://index.commoncrawl.org/CC-MAIN-'+r+'-index' for r in ret]
    else:
        raise ValueError('unknown cc_duration of %s', cc_duration)

    if cc_sort != 'mixed':
        ret.reverse()

    return ret


lines_per_page = 3000  # no way to get this from the API without fetching a page


def showNumPages(r):
    j = r.json()
    if isinstance(j, dict):  # pywb always returns json
        pages = int(j.get('blocks', 0))
    elif isinstance(j, int):  # ia always returns text, parsed as a json int
        pages = j
    else:
        raise ValueError('surprised by showNumPages value of '+str(j))
    return pages


def pages_to_samples(pages):
    # adjust pages for the partial page at the start and end
    if pages > 1:
        pages = pages - 1.0
    elif pages >= 1:
        pages = pages - 0.5
    pages *= lines_per_page
    return int(pages)


def cdx_to_json(resp):
    if resp.status_code == 404:
        return []

    text = resp.text

    if text.startswith('{'):  # pywb output='json' is jsonl
        lines = resp.text.splitlines()
        ret = []
        for l in lines:
            ret.append(json.loads(l))
        return ret

    # ia output='json' is a json list of lists
    if not text.startswith('['):
        raise ValueError('cannot decode response, first bytes are'+text[:50])

    try:
        lines = json.loads(text)
        fields = lines.pop(0)  # first line is the list of field names
    except (json.decoder.JSONDecodeError, KeyError):
        raise ValueError('cannot decode response, first bytes are'+text[:50])

    ret = []
    for l in lines:
        obj = {}
        for f in fields:
            obj[f] = l.pop(0)
        ret.append(obj)
    return ret


class CDXFetcherIter:
    def __init__(self, cdxfetcher, params={}):
        self.cdxfetcher = cdxfetcher
        self.params = params
        if 'page' in params:
            raise ValueError('must not set page= in a CDX iterator')
        self.endpoint = 0
        self.page = -1
        self.params['page'] = self.page
        self.cdx_objs = []

        self.get_more()

    def get_more(self):
        while True:
            self.page += 1
            status, objs = self.cdxfetcher.get_for_iter(self.endpoint, self.page, params=self.params)
            if status == 'last endpoint':
                LOGGER.debug('get_more: I have reached the end')
                return  # caller will raise StopIteration
            if status == 'last page':
                LOGGER.debug('get_more: moving to next endpoint')
                self.endpoint += 1
                self.page = -1
                continue
            LOGGER.debug('get_more, got %d more objs', len(objs))
            self.cdx_objs.extend(objs)

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            try:
                return self.cdx_objs.pop(0)
            except IndexError:
                LOGGER.debug('getting more in __next__')
                self.get_more()
                if len(self.cdx_objs) <= 0:
                    raise StopIteration


class CDXFetcher:
    def __init__(self, source='cc', cc_duration='365d', cc_sort='mixed'):
        self.source = source
        self.cc_duration = cc_duration
        self.cc_sort = cc_sort

        if source == 'cc':
            self.index_list = get_cc_endpoints(cc_duration, cc_sort)
        elif source == 'ia':
            self.index_list = ('https://web.archive.org/cdx/search/cdx',)
        elif source.startswith('https://') or source.startswith('http://'):
            self.index_list = (source,)
        else:
            raise ValueError('could not understand source')

    def get(self, url, **kwargs):
        # from_ts=None, to=None, matchType=None, limit=None, sort=None, closest=None,
        # filter=None, fl=None, page=None, pageSize=None, showNumPages=None):
        params = kwargs
        params['url'] = url
        params['output'] = 'json'  # XXX document me
        if 'limit' not in params:
            params['limit'] = 10000  # XXX document me

        ret = []
        for endpoint in self.index_list:
            resp = myrequests_get(endpoint, params=params)
            objs = cdx_to_json(resp)  # turns 400 and 404 into []
            ret.extend(objs)
            if 'limit' in params:
                params['limit'] -= len(objs)
                if params['limit'] <= 0:
                    break
        return ret

    def items(self, url, **kwargs):
        params = kwargs
        params['url'] = url
        params['output'] = 'json'
        # XXX document me, limit not set
        return CDXFetcherIter(self, params=params)

    def get_for_iter(self, endpoint, page, params={}):
        '''
        Specalized get for the iterator
        '''
        if endpoint >= len(self.index_list):
            return 'last endpoint', []
        if params.get('limit', -1) == 0:
            return 'last endpoint', []  # a little white lie

        endpoint = self.index_list[endpoint]
        params['page'] = page
        resp = myrequests_get(endpoint, params=params)
        if resp.status_code == 400:
            return 'last page', []

        ret = cdx_to_json(resp)  # turns 404 into []
        if 'limit' in params:
            params['limit'] -= len(ret)
        return 'ok', ret

    def get_size_estimate(self, url, as_pages=False, **kwargs):
        '''
        Get the number of pages that match url

        useful additional args: matchType='host' pageSize=1
        or, url can end with * or start with *. to set the matchType
        '''

        params = {'url': url, 'showNumPages': 'true'}
        params.update(**kwargs)

        pages = 0
        for endpoint in self.index_list:
            resp = myrequests_get(endpoint, params=params)
            if resp.status_code == 200:
                pages += showNumPages(resp)
            else:
                pass  # silently ignore empty answers

        if not as_pages:
            pages = pages_to_samples(pages)
        return pages