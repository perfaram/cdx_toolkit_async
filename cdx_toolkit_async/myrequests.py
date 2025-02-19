import httpx
import anyio
import logging
import time
import contextvars
from urllib.parse import urlparse
import httpcore

specific_transport_error_types = []
try:
    import trio
    specific_transport_error_types += [trio.BrokenResourceError]
except:
    pass

try:
    import python_socks
    specific_transport_error_types += [python_socks._errors.ProxyTimeoutError, python_socks._errors.ProxyError]
except:
    pass

from . import __version__

LOGGER = logging.getLogger(__name__)
async_httpx_client = contextvars.ContextVar("async_httpx_client")

previously_seen_hostnames = {
    'commoncrawl.s3.amazonaws.com',
    'data.commoncrawl.org',
    'web.archive.org',
}


def dns_fatal(url):
    '''We have a dns error, should we fail immediately or not?'''
    hostname = urlparse(url).hostname
    if hostname not in previously_seen_hostnames:
        return True

def myrequests_get_prepare_params(params=None, headers=None):
    if params:
        if 'from_ts' in params:
            params['from'] = params['from_ts']
            del params['from_ts']
        if 'limit' in params:
            if not isinstance(params['limit'], int):
                # this needs to be an int because we subtract from it elsewhere
                params['limit'] = int(params['limit'])

    if headers is None:
        headers = {}
    if 'user-agent' not in headers:
        headers['User-Agent'] = 'pypi_cdx_toolkit_async/'+__version__

    return params, headers

async def myrequests_get_handle_response(resp, retries: int, cdx=False, allow404=False, expected_status=None):
    if cdx and resp.status_code in {400, 404}:
        # 400: ia html error page -- probably page= is too big -- not an error
        # 404: pywb {'error': 'No Captures found for: www.pbxxxxxxm.com/*'} -- not an error
        LOGGER.debug('giving up with status %d, no captures found', resp.status_code)
        return False, retries
    if allow404 and resp.status_code == 404:
        return False, retries
    if expected_status is not None and resp.status_code == expected_status:
        return False, retries
    if resp.status_code in {429, 500, 502, 503, 504, 509}:  # pragma: no cover
        # 503=slow down, 50[24] are temporary outages, 500=Amazon S3 generic error
        # CC takes a 503 from storage and then emits a 500 with error text in resp.text
        # I have never seen IA or CC send 429 or 509, but just in case...
        retries += 1
        if retries > 5:
            LOGGER.warning('retrying after 1s for %d', resp.status_code)
            if resp.text:
                LOGGER.debug('response body is %s', resp.text)
        else:
            LOGGER.info('retrying after 1s for %d', resp.status_code)
            if resp.text:
                LOGGER.debug('response body is %s', resp.text)
        await anyio.sleep(1)
        return True, retries
    if resp.status_code in {400, 404}:  # pragma: no cover
        if resp.text:
            LOGGER.debug('response body is %s', resp.text)
        raise RuntimeError('invalid url of some sort, status={} {}'.format(resp.status_code, resp.url))
    if 300 <= resp.status_code and resp.status_code < 400:
        return False, retries
    resp.raise_for_status()
    return False, retries

async def myrequests_get_handle_error(e, connect_errors, url, params):
    connect_errors += 1
    string = '{} failures for url {} {!r}: {}'.format(connect_errors, url, params, str(e))

    if 'Name or service not known' in string:
        if dns_fatal(url):
            raise ValueError('invalid hostname in url '+url) from None

    if connect_errors > 100:
        LOGGER.error(string)
        raise ValueError(string)
    if connect_errors > 10:
        LOGGER.warning(string)
    LOGGER.info('retrying after 1s for '+str(e))
    await anyio.sleep(1)
    return connect_errors

def myrequests_get_update_seen_hostnames(url):
    hostname = urlparse(url).hostname
    if hostname not in previously_seen_hostnames:
        previously_seen_hostnames.add(hostname)

async def myrequests_get(url, session=None, params=None, headers=None, cdx=False, allow404=False, expect_status=None):
    session = session if session else async_httpx_client.get()

    params, headers = myrequests_get_prepare_params(params=params, headers=headers)

    retry = True
    retries = 0
    connect_errors = 0
    while retry:
        try:
            LOGGER.debug('getting %s %r', url, params)
            resp = await session.get(url, params=params, headers=headers,
                                timeout=(30., 30.), follow_redirects=False)
            retry, retries = await myrequests_get_handle_response(resp, retries, cdx, allow404, expect_status)
            if not retry:
                break
            else:
                continue
        except (httpx.TransportError, 
                httpx.DecodingError, 
                *specific_transport_error_types) as e:
            connect_errors = await myrequests_get_handle_error(e, connect_errors, url, params)
        except (httpx.HTTPError, httpcore.ProtocolError) as e:  # pragma: no cover
            LOGGER.warning('something unexpected happened, giving up after %s', str(e))
            connect_errors += 1
            if connect_errors >= 5:
                raise


    myrequests_get_update_seen_hostnames(url)

    return resp
