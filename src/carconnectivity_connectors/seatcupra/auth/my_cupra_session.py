"""
Module implements the MyCupra Session handling.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

import json
import logging
import secrets

from urllib.parse import parse_qsl, urlparse

import requests
from requests.models import CaseInsensitiveDict

from oauthlib.common import add_params_to_uri, generate_nonce, to_unicode
from oauthlib.oauth2 import InsecureTransportError
from oauthlib.oauth2 import is_secure_transport

from carconnectivity.errors import AuthenticationError, RetrievalError, TemporaryAuthenticationError

from carconnectivity_connectors.seatcupra.auth.openid_session import AccessType
from carconnectivity_connectors.seatcupra.auth.vw_web_session import VWWebSession

if TYPE_CHECKING:
    from typing import Tuple, Dict, Any


LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.seatcupra.auth")


class MyCupraSession(VWWebSession):
    """
    MyCupraSession class handles the authentication and session management for Cupras's MyCupra service.
    """
    def __init__(self, session_user, is_seat: bool, **kwargs) -> None:
        self.is_seat: bool = is_seat
        if self.is_seat:
            super(MyCupraSession, self).__init__(client_id='99a5b77d-bd88-4d53-b4e5-a539c60694a3@apps_vw-dilab_com',
                                                refresh_url='https://identity.vwgroup.io/oidc/v1/token',
                                                scope='openid profile nickname birthdate phone',
                                                redirect_uri='seat://oauth-callback',
                                                state=None,
                                                session_user=session_user,
                                                **kwargs)

            self.headers = CaseInsensitiveDict({
                'accept': '*/*',
                'connection': 'keep-alive',
                'content-type': 'application/json',
                'user-agent': 'SEATApp/2.5.0 (com.seat.myseat.ola; build:202410171614; iOS 15.8.3) Alamofire/5.7.0 Mobile',
                'accept-language': 'de-de',
                'accept-encoding': 'gzip, deflate, br'
            })
        else:
            super(MyCupraSession, self).__init__(client_id='3c756d46-f1ba-4d78-9f9a-cff0d5292d51@apps_vw-dilab_com',
                                                refresh_url='https://identity.vwgroup.io/oidc/v1/token',
                                                scope='openid profile nickname birthdate phone',
                                                redirect_uri='cupra://oauth-callback',
                                                state=None,
                                                session_user=session_user,
                                                **kwargs)

            self.headers = CaseInsensitiveDict({
                'accept': '*/*',
                'connection': 'keep-alive',
                'content-type': 'application/json',
                'user-agent': 'CUPRAApp%20-%20Store/20220503 CFNetwork/1333.0.4 Darwin/21.5.0',
                'accept-language': 'de-de',
                'accept-encoding': 'gzip, deflate, br'
            })

    def login(self):
        super(MyCupraSession, self).login()
        # retrieve authorization URL
        authorization_url_str: str = self.authorization_url(url='https://identity.vwgroup.io/oidc/v1/authorize')
        if self.redirect_uri is not None and authorization_url_str.startswith(self.redirect_uri):
            response = authorization_url_str.replace(self.redirect_uri + '#', 'https://egal?')
        else:
            # perform web authentication
            response = self.do_web_auth(authorization_url_str)
        # fetch tokens from web authentication response
        if self.is_seat:
            self.fetch_tokens('https://ola.prod.code.seat.cloud.vwgroup.com/authorization/api/v1/token',
                              authorization_response=response)
        else:
            self.fetch_tokens('https://identity.vwgroup.io/oidc/v1/token',
                              authorization_response=response)

    def refresh(self) -> None:
        # refresh tokens from refresh endpoint
        if self.is_seat:
            self.refresh_tokens('https://ola.prod.code.seat.cloud.vwgroup.com/authorization/api/v1/token')
        else:
            self.refresh_tokens('https://identity.vwgroup.io/oidc/v1/token')

    def fetch_tokens(
        self,
        token_url,
        authorization_response=None,
        **_
    ):
        """
        Fetches tokens using the given token URL using the tokens from authorization response.

        Args:
            token_url (str): The URL to request the tokens from.
            authorization_response (str, optional): The authorization response containing the tokens. Defaults to None.
            **_ : Additional keyword arguments.

        Returns:
            dict: A dictionary containing the fetched tokens if successful.
            None: If the tokens could not be fetched.

        Raises:
            TemporaryAuthenticationError: If the token request fails due to a temporary MyCupra failure.
        """
        # take token from authorization response (those are stored in self.token now!)
        self.parse_from_fragment(authorization_response)

        if self.token is not None and all(key in self.token for key in ('state', 'id_token', 'access_token', 'code')):
            # Generate json body for token request
            if self.is_seat:
                body: Dict[str, Any] = {'state': self.token['state'],
                                        'id_token': self.token['id_token'],
                                        'redirect_uri': self.redirect_uri,
                                        'client_id': self.client_id,
                                        'code': self.token['code'],
                                        'grant_type': 'authorization_code'
                                        }
            else:
                body: Dict[str, Any] = {'state': self.token['state'],
                                        'id_token': self.token['id_token'],
                                        'redirect_uri': self.redirect_uri,
                                        'client_id': self.client_id,
                                        'client_secret': 'eb8814e641c81a2640ad62eeccec11c98effc9bccd4269ab7af338b50a94b3a2',
                                        'code': self.token['code'],
                                        'grant_type': 'authorization_code'
                                        }

            request_headers: CaseInsensitiveDict = dict(self.headers)  # pyright: ignore reportAssignmentType
            request_headers['content-type'] = 'application/x-www-form-urlencoded; charset=utf-8'

            # request tokens from token_url
            token_response = self.post(token_url, headers=request_headers, data=body, allow_redirects=False,
                                       access_type=AccessType.NONE)  # pyright: ignore reportCallIssue
            if token_response.status_code != requests.codes['ok']:
                raise TemporaryAuthenticationError(f'Token could not be fetched due to temporary MyCupra failure: {token_response.status_code}')
            # parse token from response body
            token = self.parse_from_body(token_response.text)

            return token
        return None

    def parse_from_body(self, token_response, state=None):
        """
            Fix strange token naming before parsing it with OAuthlib.
        """
        try:
            # Tokens are in body of response in json format
            token = json.loads(token_response)
        except json.decoder.JSONDecodeError as err:
            raise TemporaryAuthenticationError('Token could not be refreshed due to temporary MyCupra failure: json could not be decoded') from err
        # Fix token keys, we want access_token instead of accessToken
        if 'accessToken' in token:
            token['access_token'] = token.pop('accessToken')
        # Fix token keys, we want id_token instead of idToken
        if 'idToken' in token:
            token['id_token'] = token.pop('idToken')
        # Fix token keys, we want refresh_token instead of refreshToken
        if 'refreshToken' in token:
            token['refresh_token'] = token.pop('refreshToken')
        # generate json from fixed dict
        fixed_token_response = to_unicode(json.dumps(token)).encode("utf-8")
        # Let OAuthlib parse the token
        return super(MyCupraSession, self).parse_from_body(token_response=fixed_token_response, state=state)

    def refresh_tokens(
        self,
        token_url,
        refresh_token=None,
        auth=None,
        timeout=None,
        headers=None,
        verify=True,
        proxies=None,
        **_
    ):
        """
        Refreshes the authentication tokens using the provided refresh token.
        Args:
            token_url (str): The URL to request new tokens from.
            refresh_token (str, optional): The refresh token to use. Defaults to None.
            auth (tuple, optional): Authentication credentials. Defaults to None.
            timeout (float or tuple, optional): How long to wait for the server to send data before giving up. Defaults to None.
            headers (dict, optional): Headers to include in the request. Defaults to None.
            verify (bool, optional): Whether to verify the server's TLS certificate. Defaults to True.
            proxies (dict, optional): Proxies to use for the request. Defaults to None.
            **_ (dict): Additional arguments.
        Raises:
            ValueError: If no token endpoint is set for auto_refresh.
            InsecureTransportError: If the token URL is not secure.
            AuthenticationError: If the server requests new authorization.
            TemporaryAuthenticationError: If the token could not be refreshed due to a temporary server failure.
            RetrievalError: If the status code from the server is not recognized.
        Returns:
            dict: The new tokens.
        """
        LOG.info('Refreshing tokens')
        if not token_url:
            raise ValueError("No token endpoint set for auto_refresh.")

        if not is_secure_transport(token_url):
            raise InsecureTransportError()

        # Store old refresh token in case no new one is given
        refresh_token = refresh_token or self.refresh_token
        if refresh_token is None:
            self.login()
            return self.token

        if headers is None:
            headers = dict(self.headers)

        if self.is_seat:
            body: Dict[str, str] = {
                'client_id': self.client_id,
                'grant_type': 'refresh_token',
                'refresh_token': self.refresh_token
            }
        else:
            body: Dict[str, str] = {
                'client_id': self.client_id,
                'client_secret': 'eb8814e641c81a2640ad62eeccec11c98effc9bccd4269ab7af338b50a94b3a2',
                'grant_type': 'refresh_token',
                'refresh_token': self.refresh_token
            }

        headers['content-type'] = 'application/x-www-form-urlencoded; charset=utf-8'

        tries = 0
        while True:
            try:
                # Request new tokens using the refresh token
                token_response = self.post(
                    token_url,
                    data=body,
                    auth=auth,
                    timeout=timeout,
                    headers=headers,
                    verify=verify,
                    withhold_token=False,  # pyright: ignore reportCallIssue
                    proxies=proxies,
                    access_type=AccessType.NONE  # pyright: ignore reportCallIssue
                )
            except requests.exceptions.RequestException as err:
                tries += 1
                if tries >= 3:
                    raise TemporaryAuthenticationError('Token could not be refreshed due to temporary MyCupra failure') from err
            else:
                break
        if token_response.status_code == requests.codes['unauthorized']:
            raise AuthenticationError('Refreshing tokens failed: Server requests new authorization')
        elif token_response.status_code in (requests.codes['internal_server_error'], requests.codes['service_unavailable'], requests.codes['gateway_timeout']):
            raise TemporaryAuthenticationError('Token could not be refreshed due to temporary MyCupra failure: {tokenResponse.status_code}')
        elif token_response.status_code == requests.codes['ok']:
            # parse new tokens from response
            self.parse_from_body(token_response.text)
            if self.token is not None and "refresh_token" not in self.token:
                LOG.debug("No new refresh token given. Re-using old.")
                self.token["refresh_token"] = refresh_token
            return self.token
        else:
            raise RetrievalError(f'Status Code from MyCupra while refreshing tokens was: {token_response.status_code}')

    def request(
        self,
        method,
        url,
        data=None,
        headers=None,
        withhold_token=False,
        access_type=AccessType.ACCESS,
        token=None,
        timeout=None,
        **kwargs
    ) -> requests.Response:
        """Intercept all requests and add userId if present."""
        if not is_secure_transport(url):
            raise InsecureTransportError()
        if self.user_id is not None:
            headers = headers or {}
            headers['user-id'] = self.user_id

        return super(MyCupraSession, self).request(method, url, headers=headers, data=data, withhold_token=withhold_token, access_type=access_type, token=token,
                                                   timeout=timeout, **kwargs)
