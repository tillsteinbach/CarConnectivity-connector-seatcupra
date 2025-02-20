"""Module implements the connector to interact with the Cupra API."""
from __future__ import annotations
from typing import TYPE_CHECKING

import threading

import json
import os
import logging
import netrc
from datetime import datetime, timezone, timedelta
import requests

from carconnectivity.garage import Garage
from carconnectivity.errors import AuthenticationError, TooManyRequestsError, RetrievalError, APIError, APICompatibilityError, \
    TemporaryAuthenticationError, SetterError, CommandError
from carconnectivity.util import robust_time_parse, log_extra_keys, config_remove_credentials
from carconnectivity.units import Length, Power, Speed
from carconnectivity.vehicle import GenericVehicle
from carconnectivity.doors import Doors
from carconnectivity.windows import Windows
from carconnectivity.lights import Lights
from carconnectivity.drive import GenericDrive, ElectricDrive, CombustionDrive
from carconnectivity.attributes import BooleanAttribute, DurationAttribute, GenericAttribute, TemperatureAttribute
from carconnectivity.units import Temperature
from carconnectivity.command_impl import ClimatizationStartStopCommand, WakeSleepCommand, HonkAndFlashCommand, LockUnlockCommand, ChargingStartStopCommand
from carconnectivity.climatization import Climatization
from carconnectivity.commands import Commands
from carconnectivity.charging import Charging

from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.cupra.auth.session_manager import SessionManager, SessionUser, Service
from carconnectivity_connectors.cupra.auth.my_cupra_session import MyCupraSession
from carconnectivity_connectors.cupra._version import __version__

SUPPORT_IMAGES = False
try:
    from PIL import Image
    import base64
    import io
    SUPPORT_IMAGES = True
    from carconnectivity.attributes import ImageAttribute
except ImportError:
    pass

if TYPE_CHECKING:
    from typing import Dict, List, Optional, Any, Union

    from carconnectivity.carconnectivity import CarConnectivity

LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.cupra")
LOG_API: logging.Logger = logging.getLogger("carconnectivity.connectors.cupra-api-debug")


# pylint: disable=too-many-lines
class Connector(BaseConnector):
    """
    Connector class for Cupra API connectivity.
    Args:
        car_connectivity (CarConnectivity): An instance of CarConnectivity.
        config (Dict): Configuration dictionary containing connection details.
    Attributes:
        max_age (Optional[int]): Maximum age for cached data in seconds.
    """
    def __init__(self, connector_id: str, car_connectivity: CarConnectivity, config: Dict) -> None:
        BaseConnector.__init__(self, connector_id=connector_id, car_connectivity=car_connectivity, config=config, log=LOG, api_log=LOG_API)

        self._background_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self.connected: BooleanAttribute = BooleanAttribute(name="connected", parent=self, tags={'connector_custom'})
        self.interval: DurationAttribute = DurationAttribute(name="interval", parent=self, tags={'connector_custom'})
        self.commands: Commands = Commands(parent=self)

        LOG.info("Loading cupra connector with config %s", config_remove_credentials(config))

        if 'spin' in config and config['spin'] is not None:
            self.active_config['spin'] = config['spin']
        else:
            self.active_config['spin'] = None

        self.active_config['username'] = None
        self.active_config['password'] = None
        if 'username' in config and 'password' in config:
            self.active_config['username'] = config['username']
            self.active_config['password'] = config['password']
        else:
            if 'netrc' in config:
                self.active_config['netrc'] = config['netrc']
            else:
                self.active_config['netrc'] = os.path.join(os.path.expanduser("~"), ".netrc")
            try:
                secrets = netrc.netrc(file=self.active_config['netrc'])
                secret: tuple[str, str, str] | None = secrets.authenticators("cupra")
                if secret is None:
                    raise AuthenticationError(f'Authentication using {self.active_config["netrc"]} failed: cupra not found in netrc')
                self.active_config['username'], account, self.active_config['password'] = secret

                if self.active_config['spin'] is None and account is not None:
                    try:
                        self.active_config['spin'] = account
                    except ValueError as err:
                        LOG.error('Could not parse spin from netrc: %s', err)
            except netrc.NetrcParseError as err:
                LOG.error('Authentification using %s failed: %s', self.active_config['netrc'], err)
                raise AuthenticationError(f'Authentication using {self.active_config["netrc"]} failed: {err}') from err
            except TypeError as err:
                if 'username' not in config:
                    raise AuthenticationError(f'"cupra" entry was not found in {self.active_config["netrc"]} netrc-file.'
                                              ' Create it or provide username and password in config') from err
            except FileNotFoundError as err:
                raise AuthenticationError(f'{self.active_config["netrc"]} netrc-file was not found. Create it or provide username and password in config') \
                                          from err

        self.active_config['interval'] = 300
        if 'interval' in config:
            self.active_config['interval'] = config['interval']
            if self.active_config['interval'] < 180:
                raise ValueError('Intervall must be at least 180 seconds')
        self.active_config['max_age'] = self.active_config['interval'] - 1
        if 'max_age' in config:
            self.active_config['max_age'] = config['max_age']
        self.interval._set_value(timedelta(seconds=self.active_config['interval']))  # pylint: disable=protected-access

        if self.active_config['username'] is None or self.active_config['password'] is None:
            raise AuthenticationError('Username or password not provided')

        self._manager: SessionManager = SessionManager(tokenstore=car_connectivity.get_tokenstore(), cache=car_connectivity.get_cache())
        session: requests.Session = self._manager.get_session(Service.MY_CUPRA, SessionUser(username=self.active_config['username'],
                                                                                              password=self.active_config['password']))
        if not isinstance(session, MyCupraSession):
            raise AuthenticationError('Could not create session')
        self.session: MyCupraSession = session
        self.session.retries = 3
        self.session.timeout = 180
        self.session.refresh()

        self._elapsed: List[timedelta] = []

    def startup(self) -> None:
        self._background_thread = threading.Thread(target=self._background_loop, daemon=False)
        self._background_thread.start()

    def _background_loop(self) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            interval = 300
            try:
                try:
                    self.fetch_all()
                    self.last_update._set_value(value=datetime.now(tz=timezone.utc))  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval: float = self.interval.value.total_seconds()
                except Exception:
                    self.connected._set_value(value=False)  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval: float = self.interval.value.total_seconds()
                    raise
            except TooManyRequestsError as err:
                LOG.error('Retrieval error during update. Too many requests from your account (%s). Will try again after 15 minutes', str(err))
                self._stop_event.wait(900)
            except RetrievalError as err:
                LOG.error('Retrieval error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            except APICompatibilityError as err:
                LOG.error('API compatability error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            except TemporaryAuthenticationError as err:
                LOG.error('Temporary authentification error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            else:
                self.connected._set_value(value=True)  # pylint: disable=protected-access
                self._stop_event.wait(interval)

    def persist(self) -> None:
        """
        Persists the current state using the manager's persist method.

        This method calls the `persist` method of the `_manager` attribute to save the current state.
        """
        self._manager.persist()

    def shutdown(self) -> None:
        """
        Shuts down the connector by persisting current state, closing the session,
        and cleaning up resources.

        This method performs the following actions:
        1. Persists the current state.
        2. Closes the session.
        3. Sets the session and manager to None.
        4. Calls the shutdown method of the base connector.

        Returns:
            None
        """
        # Disable and remove all vehicles managed soley by this connector
        for vehicle in self.car_connectivity.garage.list_vehicles():
            if len(vehicle.managing_connectors) == 1 and self in vehicle.managing_connectors:
                self.car_connectivity.garage.remove_vehicle(vehicle.id)
                vehicle.enabled = False
        self._stop_event.set()
        if self._background_thread is not None:
            self._background_thread.join()
        self.persist()
        self.session.close()
        BaseConnector.shutdown(self)

    def fetch_all(self) -> None:
        """
        Fetches all necessary data for the connector.

        This method calls the `fetch_vehicles` method to retrieve vehicle data.
        """
        self.fetch_vehicles()
        self.car_connectivity.transaction_end()

    def fetch_vehicles(self) -> None:
        """
        Fetches the list of vehicles from the Skoda Connect API and updates the garage with new vehicles.
        This method sends a request to the Skoda Connect API to retrieve the list of vehicles associated with the user's account.
        If new vehicles are found in the response, they are added to the garage.

        Returns:
            None
        """
        garage: Garage = self.car_connectivity.garage
        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v2/users/{self.session.user_id}/garage/vehicles'
        data: Dict[str, Any] | None = self._fetch_data(url, session=self.session)
        print(data)

        seen_vehicle_vins: set[str] = set()
        if data is not None:
            if 'data' in data and data['data'] is not None:
                for vehicle_dict in data['data']:
                    if 'vin' in vehicle_dict and vehicle_dict['vin'] is not None:
                        seen_vehicle_vins.add(vehicle_dict['vin'])
                        vehicle: Optional[GenericVehicle] = garage.get_vehicle(vehicle_dict['vin'])  # pyright: ignore[reportAssignmentType]
                        if vehicle is None:
                            vehicle = GenericVehicle(vin=vehicle_dict['vin'], garage=garage, managing_connector=self)
                            garage.add_vehicle(vehicle_dict['vin'], vehicle)

                        if 'nickname' in vehicle_dict and vehicle_dict['nickname'] is not None:
                            vehicle.name._set_value(vehicle_dict['nickname'])  # pylint: disable=protected-access
                        else:
                            vehicle.name._set_value(None)  # pylint: disable=protected-access

                        if 'model' in vehicle_dict and vehicle_dict['model'] is not None:
                            vehicle.model._set_value(vehicle_dict['model'])  # pylint: disable=protected-access
                        else:
                            vehicle.model._set_value(None)  # pylint: disable=protected-access

                        if 'capabilities' in vehicle_dict and vehicle_dict['capabilities'] is not None:
                            found_capabilities = set()
                            for capability_dict in vehicle_dict['capabilities']:
                                if 'id' in capability_dict and capability_dict['id'] is not None:
                                    capability_id = capability_dict['id']
                                    found_capabilities.add(capability_id)
                                    if vehicle.capabilities.has_capability(capability_id):
                                        capability: Capability = vehicle.capabilities.get_capability(capability_id)  # pyright: ignore[reportAssignmentType]
                                    else:
                                        capability = Capability(capability_id=capability_id, capabilities=vehicle.capabilities)
                                        vehicle.capabilities.add_capability(capability_id, capability)
                                    if 'expirationDate' in capability_dict and capability_dict['expirationDate'] is not None:
                                        expiration_date: datetime = robust_time_parse(capability_dict['expirationDate'])
                                        capability.expiration_date._set_value(expiration_date)  # pylint: disable=protected-access
                                    else:
                                        capability.expiration_date._set_value(None)  # pylint: disable=protected-access
                                    if 'userDisablingAllowed' in capability_dict and capability_dict['userDisablingAllowed'] is not None:
                                        # pylint: disable-next=protected-access
                                        capability.user_disabling_allowed._set_value(capability_dict['userDisablingAllowed'])
                                    else:
                                        capability.user_disabling_allowed._set_value(None)  # pylint: disable=protected-access
                                else:
                                    raise APIError('Could not fetch capabilities, capability ID missing')
                            for capability_id in vehicle.capabilities.capabilities.keys() - found_capabilities:
                                vehicle.capabilities.remove_capability(capability_id)
                        else:
                            vehicle.capabilities.clear_capabilities()

                        if vehicle.capabilities.has_capability('vehicleWakeUpTrigger'):
                            if vehicle.commands is not None and vehicle.commands.commands is not None \
                                    and not vehicle.commands.contains_command('wake-sleep'):
                                wake_sleep_command = WakeSleepCommand(parent=vehicle.commands)
                                wake_sleep_command._add_on_set_hook(self.__on_wake_sleep)  # pylint: disable=protected-access
                                wake_sleep_command.enabled = True
                                vehicle.commands.add_command(wake_sleep_command)

                        # Add honkAndFlash command if necessary capabilities are available
                        if vehicle.capabilities.has_capability('honkAndFlash'):
                            if vehicle.commands is not None and vehicle.commands.commands is not None \
                                    and not vehicle.commands.contains_command('honk-flash'):
                                honk_flash_command = HonkAndFlashCommand(parent=vehicle.commands, with_duration=True)
                                honk_flash_command._add_on_set_hook(self.__on_honk_flash)  # pylint: disable=protected-access
                                honk_flash_command.enabled = True
                                vehicle.commands.add_command(honk_flash_command)

                        # Add lock and unlock command
                        if vehicle.capabilities.has_capability('access'):
                            if vehicle.doors is not None and vehicle.doors.commands is not None and vehicle.doors.commands.commands is not None \
                                    and not vehicle.doors.commands.contains_command('lock-unlock'):
                                lock_unlock_command = LockUnlockCommand(parent=vehicle.doors.commands)
                                lock_unlock_command._add_on_set_hook(self.__on_lock_unlock)  # pylint: disable=protected-access
                                lock_unlock_command.enabled = True
                                vehicle.doors.commands.add_command(lock_unlock_command)

                        self.fetch_vehicle_status(vehicle)
                        if vehicle.capabilities.has_capability('parkingPosition'):
                            self.fetch_parking_position(vehicle)

                        if SUPPORT_IMAGES:
                            # fetch vehcile images
                            url: str = f'https://emea.bff.cariad.digital/media/v2/vehicle-images/{vehicle_dict["vin"]}?resolution=2x'
                            data = self._fetch_data(url, session=self.session, allow_http_error=True)
                            if data is not None and 'data' in data:  # pylint: disable=too-many-nested-blocks
                                for image in data['data']:
                                    img = None
                                    cache_date = None
                                    imageurl: str = image['url']
                                    if self.active_config['max_age'] is not None and self.session.cache is not None and imageurl in self.session.cache:
                                        img, cache_date_string = self.session.cache[imageurl]
                                        img = base64.b64decode(img)  # pyright: ignore[reportPossiblyUnboundVariable]
                                        img = Image.open(io.BytesIO(img))  # pyright: ignore[reportPossiblyUnboundVariable]
                                        cache_date = datetime.fromisoformat(cache_date_string)
                                    if img is None or self.active_config['max_age'] is None \
                                            or (cache_date is not None and cache_date < (datetime.utcnow() - timedelta(seconds=self.active_config['max_age']))):
                                        try:
                                            image_download_response = self.session.get(imageurl, stream=True)
                                            if image_download_response.status_code == requests.codes['ok']:
                                                img = Image.open(image_download_response.raw)  # pyright: ignore[reportPossiblyUnboundVariable]
                                                if self.session.cache is not None:
                                                    buffered = io.BytesIO()  # pyright: ignore[reportPossiblyUnboundVariable]
                                                    img.save(buffered, format="PNG")
                                                    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")  # pyright: ignore[reportPossiblyUnboundVariable]
                                                    self.session.cache[imageurl] = (img_str, str(datetime.utcnow()))
                                            elif image_download_response.status_code == requests.codes['unauthorized']:
                                                LOG.info('Server asks for new authorization')
                                                self.session.login()
                                                image_download_response = self.session.get(imageurl, stream=True)
                                                if image_download_response.status_code == requests.codes['ok']:
                                                    img = Image.open(image_download_response.raw)  # pyright: ignore[reportPossiblyUnboundVariable]
                                                    if self.session.cache is not None:
                                                        buffered = io.BytesIO()  # pyright: ignore[reportPossiblyUnboundVariable]
                                                        img.save(buffered, format="PNG")
                                                        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")  # pyright: ignore[reportPossiblyUnboundVariable]
                                                        self.session.cache[imageurl] = (img_str, str(datetime.utcnow()))
                                        except requests.exceptions.ConnectionError as connection_error:
                                            raise RetrievalError(f'Connection error: {connection_error}') from connection_error
                                        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                                            raise RetrievalError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
                                        except requests.exceptions.ReadTimeout as timeout_error:
                                            raise RetrievalError(f'Timeout during read: {timeout_error}') from timeout_error
                                        except requests.exceptions.RetryError as retry_error:
                                            raise RetrievalError(f'Retrying failed: {retry_error}') from retry_error
                                    if img is not None:
                                        vehicle._car_images[image['id']] = img  # pylint: disable=protected-access
                                        if image['id'] == 'car_34view':
                                            if 'car_picture' in vehicle.images.images:
                                                vehicle.images.images['car_picture']._set_value(img)  # pylint: disable=protected-access
                                            else:
                                                vehicle.images.images['car_picture'] = ImageAttribute(name="car_picture", parent=vehicle.images,
                                                                                                      value=img, tags={'carconnectivity'})
                    else:
                        raise APIError('Could not fetch vehicle data, VIN missing')
        for vin in set(garage.list_vehicle_vins()) - seen_vehicle_vins:
            vehicle_to_remove = garage.get_vehicle(vin)
            if vehicle_to_remove is not None and vehicle_to_remove.is_managed_by_connector(self):
                garage.remove_vehicle(vin)

    def _record_elapsed(self, elapsed: timedelta) -> None:
        """
        Records the elapsed time.

        Args:
            elapsed (timedelta): The elapsed time to record.
        """
        self._elapsed.append(elapsed)

    def _fetch_data(self, url, session, force=False, allow_empty=False, allow_http_error=False, allowed_errors=None) -> Optional[Dict[str, Any]]:  # noqa: C901
        data: Optional[Dict[str, Any]] = None
        cache_date: Optional[datetime] = None
        if not force and (self.active_config['max_age'] is not None and session.cache is not None and url in session.cache):
            data, cache_date_string = session.cache[url]
            cache_date = datetime.fromisoformat(cache_date_string)
        if data is None or self.active_config['max_age'] is None \
                or (cache_date is not None and cache_date < (datetime.utcnow() - timedelta(seconds=self.active_config['max_age']))):
            try:
                status_response: requests.Response = session.get(url, allow_redirects=False)
                self._record_elapsed(status_response.elapsed)
                if status_response.status_code in (requests.codes['ok'], requests.codes['multiple_status']):
                    data = status_response.json()
                    if session.cache is not None:
                        session.cache[url] = (data, str(datetime.utcnow()))
                elif status_response.status_code == requests.codes['too_many_requests']:
                    raise TooManyRequestsError('Could not fetch data due to too many requests from your account. '
                                               f'Status Code was: {status_response.status_code}')
                elif status_response.status_code == requests.codes['unauthorized']:
                    LOG.info('Server asks for new authorization')
                    session.login()
                    status_response = session.get(url, allow_redirects=False)

                    if status_response.status_code in (requests.codes['ok'], requests.codes['multiple_status']):
                        data = status_response.json()
                        if session.cache is not None:
                            session.cache[url] = (data, str(datetime.utcnow()))
                    elif not allow_http_error or (allowed_errors is not None and status_response.status_code not in allowed_errors):
                        raise RetrievalError(f'Could not fetch data even after re-authorization. Status Code was: {status_response.status_code}')
                elif not allow_http_error or (allowed_errors is not None and status_response.status_code not in allowed_errors):
                    print(status_response.request.headers)
                    raise RetrievalError(f'Could not fetch data. Status Code was: {status_response.status_code}')
            except requests.exceptions.ConnectionError as connection_error:
                raise RetrievalError(f'Connection error: {connection_error}') from connection_error
            except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                raise RetrievalError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
            except requests.exceptions.ReadTimeout as timeout_error:
                raise RetrievalError(f'Timeout during read: {timeout_error}') from timeout_error
            except requests.exceptions.RetryError as retry_error:
                raise RetrievalError(f'Retrying failed: {retry_error}') from retry_error
            except requests.exceptions.JSONDecodeError as json_error:
                if allow_empty:
                    data = None
                else:
                    raise RetrievalError(f'JSON decode error: {json_error}') from json_error
        return data

    def get_version(self) -> str:
        return __version__

    def get_type(self) -> str:
        return "carconnectivity-connector-cupra"