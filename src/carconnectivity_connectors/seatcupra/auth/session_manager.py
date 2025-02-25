"""Module implementing the SessionManager class."""
from __future__ import annotations
from typing import TYPE_CHECKING, Tuple

from enum import Enum

import hashlib

import logging

from carconnectivity_connectors.seatcupra.auth.my_cupra_session import MyCupraSession

if TYPE_CHECKING:
    from typing import Dict, Any
    from carconnectivity_connectors.seatcupra.auth.vw_web_session import VWWebSession

LOG = logging.getLogger("carconnectivity.connectors.seatcupra.auth")


class SessionUser():
    """
    A class to represent a session user with a username and password.

    Attributes:
    ----------
    username : str
        The username of the session user.
    password : str
        The password of the session user.

    Methods:
    -------
    __str__():
        Returns a string representation of the session user in the format 'username:password'.
    """
    def __init__(self, username: str, password: str) -> None:
        self.username: str = username
        self.password: str = password

    def __str__(self) -> str:
        return f'{self.username}:{self.password}'


class Service(Enum):
    """
    An enumeration representing different services.

    Attributes:
        MY_CUPRA (str): Represents the 'MyCupra' service.
        MY_SEAT (str): Represents the 'MySeat' service.

    Methods:
        __str__() -> str: Returns the string representation of the service.
    """
    MY_CUPRA = 'MyCupra'
    MY_SEAT = 'MySeat'

    def __str__(self) -> str:
        return self.value


class SessionManager():
    """
    Manages sessions for different services and users, handling token storage and caching.
    """
    def __init__(self, tokenstore: Dict[str, Any], cache:  Dict[str, Any]) -> None:
        self.tokenstore: Dict[str, Any] = tokenstore
        self.cache: Dict[str, Any] = cache
        self.sessions: Dict[Tuple[Service, SessionUser], VWWebSession] = {}

    @staticmethod
    def generate_hash(service: Service, session_user: SessionUser) -> str:
        """
        Generates a SHA-512 hash for the given service and session user.

        Args:
            service (Service): The service for which the hash is being generated.
            session_user (SessionUser): The session user for which the hash is being generated.

        Returns:
            str: The generated SHA-512 hash as a hexadecimal string.
        """
        hash_str: str = service.value + str(session_user)
        return hashlib.sha512(hash_str.encode()).hexdigest()

    @staticmethod
    def generate_identifier(service: Service, session_user: SessionUser) -> str:
        """
        Generate a unique identifier for a given service and session user.

        Args:
            service (Service): The service for which the identifier is being generated.
            session_user (SessionUser): The session user for whom the identifier is being generated.

        Returns:
            str: A unique identifier string.
        """
        return 'CarConnectivity-connector-cupra:' + SessionManager.generate_hash(service, session_user)

    def get_session(self, service: Service, session_user: SessionUser) -> VWWebSession:
        """
        Retrieves a session for the given service and session user. If a session already exists in the sessions cache,
        it is returned. Otherwise, a new session is created using the token, metadata, and cache from the tokenstore
        and cache if available.

        Args:
            service (Service): The service for which the session is being requested.
            session_user (SessionUser): The user for whom the session is being requested.

        Returns:
            Session: The session object for the given service and session user.
        """
        session = None
        if (service, session_user) in self.sessions:
            return self.sessions[(service, session_user)]

        identifier: str = SessionManager.generate_identifier(service, session_user)
        token = None
        cache = {}
        metadata = {}

        if identifier in self.tokenstore:
            if 'token' in self.tokenstore[identifier]:
                LOG.info('Reusing tokens from previous session')
                token = self.tokenstore[identifier]['token']
            if 'metadata' in self.tokenstore[identifier]:
                metadata = self.tokenstore[identifier]['metadata']
        if identifier in self.cache:
            cache = self.cache[identifier]

        if service == Service.MY_CUPRA:
            session = MyCupraSession(session_user=session_user, is_seat=False, token=token, metadata=metadata, cache=cache)
        elif service == Service.MY_SEAT:
            session = MyCupraSession(session_user=session_user, is_seat=True, token=token, metadata=metadata, cache=cache)
        else:
            raise ValueError(f"Unsupported service: {service}")

        self.sessions[(service, session_user)] = session
        return session

    def persist(self) -> None:
        """
        Persist the current sessions into the token store and cache.

        This method iterates over the sessions and stores each session's token and metadata
        in the token store using a generated identifier. It also stores the session's cache
        in the cache.
        """
        for (service, user), session in self.sessions.items():
            identifier: str = SessionManager.generate_identifier(service, user)
            self.tokenstore[identifier] = {}
            self.tokenstore[identifier]['token'] = session.token
            self.tokenstore[identifier]['metadata'] = session.metadata
            self.cache[identifier] = session.cache
