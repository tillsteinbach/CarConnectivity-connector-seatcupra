"""Module implements the connector to interact with the Seat/Cupra API."""
from __future__ import annotations
from typing import TYPE_CHECKING

import threading

import json
import os
import traceback
import logging
import netrc
from datetime import datetime, timezone, timedelta
import requests

from carconnectivity.garage import Garage
from carconnectivity.errors import AuthenticationError, TooManyRequestsError, RetrievalError, APIError, APICompatibilityError, \
    TemporaryAuthenticationError, SetterError, CommandError
from carconnectivity.util import robust_time_parse, log_extra_keys, config_remove_credentials
from carconnectivity.units import Length, Current
from carconnectivity.doors import Doors
from carconnectivity.windows import Windows
from carconnectivity.lights import Lights
from carconnectivity.drive import GenericDrive, ElectricDrive, CombustionDrive, DieselDrive
from carconnectivity.vehicle import GenericVehicle, ElectricVehicle
from carconnectivity.attributes import BooleanAttribute, DurationAttribute, GenericAttribute, TemperatureAttribute, EnumAttribute
from carconnectivity.units import Temperature
from carconnectivity.command_impl import ClimatizationStartStopCommand, WakeSleepCommand, HonkAndFlashCommand, LockUnlockCommand, ChargingStartStopCommand, \
    WindowHeatingStartStopCommand
from carconnectivity.climatization import Climatization
from carconnectivity.commands import Commands
from carconnectivity.charging import Charging
from carconnectivity.charging_connector import ChargingConnector
from carconnectivity.position import Position
from carconnectivity.enums import ConnectionState
from carconnectivity.window_heating import WindowHeatings

from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.seatcupra.auth.session_manager import SessionManager, SessionUser, Service
from carconnectivity_connectors.seatcupra.auth.my_cupra_session import MyCupraSession
from carconnectivity_connectors.seatcupra._version import __version__
from carconnectivity_connectors.seatcupra.capability import Capability
from carconnectivity_connectors.seatcupra.vehicle import SeatCupraVehicle, SeatCupraElectricVehicle, SeatCupraCombustionVehicle, SeatCupraHybridVehicle
from carconnectivity_connectors.seatcupra.charging import SeatCupraCharging, mapping_seatcupra_charging_state
from carconnectivity_connectors.seatcupra.climatization import SeatCupraClimatization
from carconnectivity_connectors.seatcupra.command_impl import SpinCommand

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

LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.seatcupra")
LOG_API: logging.Logger = logging.getLogger("carconnectivity.connectors.seatcupra-api-debug")


# pylint: disable=too-many-lines
class Connector(BaseConnector):
    """
    Connector class for Seat/Cupra API connectivity.
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

        self.connection_state: EnumAttribute = EnumAttribute(name="connection_state", parent=self, value_type=ConnectionState,
                                                             value=ConnectionState.DISCONNECTED, tags={'connector_custom'})
        self.interval: DurationAttribute = DurationAttribute(name="interval", parent=self, tags={'connector_custom'})
        self.interval.minimum = timedelta(seconds=180)
        self.interval._is_changeable = True  # pylint: disable=protected-access

        self.commands: Commands = Commands(parent=self)

        LOG.info("Loading seatcupra connector with config %s", config_remove_credentials(config))

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
                secret: tuple[str, str, str] | None = secrets.authenticators("seatcupra")
                if secret is None:
                    raise AuthenticationError(f'Authentication using {self.active_config["netrc"]} failed: seatcupra not found in netrc')
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
                    raise AuthenticationError(f'"seatcupra" entry was not found in {self.active_config["netrc"]} netrc-file.'
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

        if 'brand' in config:
            if config['brand'] not in ['seat', 'cupra']:
                raise ValueError('Brand must be either "seat" or "cupra"')
            self.active_config['brand'] = config['brand']
        else:
            self.active_config['brand'] = 'cupra'

        if self.active_config['username'] is None or self.active_config['password'] is None:
            raise AuthenticationError('Username or password not provided')

        if self.active_config['brand'] == 'cupra':
            service = Service.MY_CUPRA
        elif self.active_config['brand'] == 'seat':
            service = Service.MY_SEAT
        else:
            raise ValueError('Brand must be either "seat" or "cupra"')
        self._manager: SessionManager = SessionManager(tokenstore=car_connectivity.get_tokenstore(), cache=car_connectivity.get_cache())
        session: requests.Session = self._manager.get_session(service, SessionUser(username=self.active_config['username'],
                                                                                   password=self.active_config['password']))
        if not isinstance(session, MyCupraSession):
            raise AuthenticationError('Could not create session')
        self.session: MyCupraSession = session
        self.session.retries = 3
        self.session.timeout = 30
        self.session.refresh()

        self._elapsed: List[timedelta] = []

    def startup(self) -> None:
        self._background_thread = threading.Thread(target=self._background_loop, daemon=False)
        self._background_thread.name = 'carconnectivity.connectors.seatcupra-background'
        self._background_thread.start()
        self.healthy._set_value(value=True)  # pylint: disable=protected-access

    def _background_loop(self) -> None:
        self._stop_event.clear()
        fetch: bool = True
        self.connection_state._set_value(value=ConnectionState.CONNECTING)  # pylint: disable=protected-access
        while not self._stop_event.is_set():
            interval = 300
            try:
                try:
                    if fetch:
                        self.fetch_all()
                        fetch = False
                    else:
                        self.update_vehicles()
                    self.last_update._set_value(value=datetime.now(tz=timezone.utc))  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval: float = self.interval.value.total_seconds()
                except Exception:
                    if self.interval.value is not None:
                        interval: float = self.interval.value.total_seconds()
                    raise
            except TooManyRequestsError as err:
                LOG.error('Retrieval error during update. Too many requests from your account (%s). Will try again after 15 minutes', str(err))
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(900)
            except RetrievalError as err:
                LOG.error('Retrieval error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
            except APIError as err:
                LOG.error('API error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
            except APICompatibilityError as err:
                LOG.error('API compatability error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
            except TemporaryAuthenticationError as err:
                LOG.error('Temporary authentification error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
            except Exception as err:
                LOG.critical('Critical error during update: %s', traceback.format_exc())
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self.healthy._set_value(value=False)  # pylint: disable=protected-access
                raise err
            else:
                self.connection_state._set_value(value=ConnectionState.CONNECTED)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
        # When leaving the loop, set the connection state to disconnected
        self.connection_state._set_value(value=ConnectionState.DISCONNECTED)  # pylint: disable=protected-access

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
        self.session.close()
        if self._background_thread is not None:
            self._background_thread.join()
        self.persist()
        BaseConnector.shutdown(self)

    def fetch_all(self) -> None:
        """
        Fetches all necessary data for the connector.

        This method calls the `fetch_vehicles` method to retrieve vehicle data.
        """
        # Add spin command
        if self.commands is not None and not self.commands.contains_command('spin'):
            spin_command = SpinCommand(parent=self.commands)
            spin_command._add_on_set_hook(self.__on_spin)  # pylint: disable=protected-access
            spin_command.enabled = True
            self.commands.add_command(spin_command)
        self.fetch_vehicles()
        self.car_connectivity.transaction_end()

    def update_vehicles(self) -> None:
        """
        Updates the status of all vehicles in the garage managed by this connector.

        This method iterates through all vehicle VINs in the garage, and for each vehicle that is
        managed by this connector and is an instance of Seat/CupraVehicle, it updates the vehicle's status
        by fetching data from various APIs. If the vehicle is an instance of Seat/CupraElectricVehicle,
        it also fetches charging information.

        Returns:
            None
        """
        garage: Garage = self.car_connectivity.garage
        for vin in set(garage.list_vehicle_vins()):
            vehicle_to_update: Optional[GenericVehicle] = garage.get_vehicle(vin)
            if vehicle_to_update is not None and vehicle_to_update.is_managed_by_connector(self) and isinstance(vehicle_to_update, SeatCupraVehicle):
                vehicle_to_update = self.fetch_vehicle_status(vehicle_to_update)
                vehicle_to_update = self.fetch_vehicle_mycar_status(vehicle_to_update)
                vehicle_to_update = self.fetch_mileage(vehicle_to_update)
                vehicle_to_update = self.fetch_ranges(vehicle_to_update)
                if vehicle_to_update.capabilities.has_capability('climatisation', check_status_ok=True):
                    vehicle_to_update = self.fetch_climatisation(vehicle_to_update)
                if vehicle_to_update.capabilities.has_capability('charging', check_status_ok=True):
                    vehicle_to_update = self.fetch_charging(vehicle_to_update)
                if vehicle_to_update.capabilities.has_capability('parkingPosition', check_status_ok=True):
                    vehicle_to_update = self.fetch_parking_position(vehicle_to_update)
                if vehicle_to_update.capabilities.has_capability('vehicleHealthInspection', check_status_ok=True):
                    vehicle_to_update = self.fetch_maintenance(vehicle_to_update)
                vehicle_to_update = self.fetch_connection_status(vehicle_to_update)
                self.decide_state(vehicle_to_update)
        self.car_connectivity.transaction_end()

    def decide_state(self, vehicle: SeatCupraVehicle) -> None:
        """
        Decides the state of the vehicle based on the current data.

        Args:
            vehicle (SeatCupraVehicle): The SeatCupra vehicle object.
        """
        if vehicle is not None:
            if vehicle.position is not None and vehicle.position.enabled and vehicle.position.position_type is not None \
                    and vehicle.position.position_type.enabled and vehicle.position.position_type.value == Position.PositionType.PARKING:
                vehicle.state._set_value(GenericVehicle.State.PARKED)  # pylint: disable=protected-access
            else:
                vehicle.state._set_value(None)  # pylint: disable=protected-access

    def fetch_vehicles(self) -> None:
        """
        Fetches the list of vehicles from the Seat/Cupra Connect API and updates the garage with new vehicles.
        This method sends a request to the Seat/Cupra Connect API to retrieve the list of vehicles associated with the user's account.
        If new vehicles are found in the response, they are added to the garage.

        Returns:
            None
        """
        garage: Garage = self.car_connectivity.garage
        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v2/users/{self.session.user_id}/garage/vehicles'
        data: Dict[str, Any] | None = self._fetch_data(url, session=self.session)

        seen_vehicle_vins: set[str] = set()
        if data is not None:
            if 'vehicles' in data and data['vehicles'] is not None:
                for vehicle_dict in data['vehicles']:
                    if 'vin' in vehicle_dict and vehicle_dict['vin'] is not None:
                        vin: str = vehicle_dict['vin']
                        seen_vehicle_vins.add(vin)
                        vehicle: Optional[GenericVehicle] = garage.get_vehicle(vin)  # pyright: ignore[reportAssignmentType]
                        if vehicle is None:
                            vehicle = SeatCupraVehicle(vin=vin, garage=garage, managing_connector=self)
                            garage.add_vehicle(vin, vehicle)

                        if 'vehicleNickname' in vehicle_dict and vehicle_dict['vehicleNickname'] is not None:
                            vehicle.name._set_value(vehicle_dict['vehicleNickname'])  # pylint: disable=protected-access
                        else:
                            vehicle.name._set_value(None)  # pylint: disable=protected-access

                        if 'specifications' in vehicle_dict and vehicle_dict['specifications'] is not None:
                            if 'steeringRight' in vehicle_dict['specifications'] and vehicle_dict['specifications']['steeringRight'] is not None:
                                if vehicle_dict['specifications']['steeringRight']:
                                    # pylint: disable-next=protected-access
                                    vehicle.specification.steering_wheel_position._set_value(GenericVehicle.VehicleSpecification.SteeringPosition.RIGHT)
                                else:
                                    # pylint: disable-next=protected-access
                                    vehicle.specification.steering_wheel_position._set_value(GenericVehicle.VehicleSpecification.SteeringPosition.LEFT)
                            else:
                                vehicle.specification.steering_wheel_position._set_value(None)  # pylint: disable=protected-access
                            if 'factoryModel' in vehicle_dict['specifications'] and vehicle_dict['specifications']['factoryModel'] is not None:
                                factory_model: Dict = vehicle_dict['specifications']['factoryModel']
                                if 'vehicleBrand' in factory_model and factory_model['vehicleBrand'] is not None:
                                    vehicle.manufacturer._set_value(factory_model['vehicleBrand'])  # pylint: disable=protected-access
                                else:
                                    vehicle.manufacturer._set_value(None)  # pylint: disable=protected-access
                                if 'vehicleModel' in factory_model and factory_model['vehicleModel'] is not None:
                                    vehicle.model._set_value(factory_model['vehicleModel'])  # pylint: disable=protected-access
                                else:
                                    vehicle.model._set_value(None)  # pylint: disable=protected-access
                                if 'modYear' in factory_model and factory_model['modYear'] is not None:
                                    vehicle.model_year._set_value(factory_model['modYear'])  # pylint: disable=protected-access
                                else:
                                    vehicle.model_year._set_value(None)  # pylint: disable=protected-access
                                log_extra_keys(LOG_API, 'factoryModel', factory_model,  {'vehicleBrand', 'vehicleModel', 'modYear'})
                            log_extra_keys(LOG_API, 'specifications', vehicle_dict['specifications'],  {'steeringRight', 'factoryModel'})

                        if isinstance(vehicle, SeatCupraVehicle):
                            url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v2/vehicles/{vin}/capabilities'
                            capabilities_data: Dict[str, Any] | None = self._fetch_data(url, session=self.session)
                            if capabilities_data is not None and 'capabilities' in capabilities_data and capabilities_data['capabilities'] is not None:
                                found_capabilities = set()
                                for capability_dict in capabilities_data['capabilities']:
                                    if 'id' in capability_dict and capability_dict['id'] is not None:
                                        capability_id = capability_dict['id']
                                        found_capabilities.add(capability_id)
                                        if vehicle.capabilities.has_capability(capability_id):
                                            capability: Capability = vehicle.capabilities.get_capability(capability_id)  # pyright: ignore[reportAssignmentType]
                                        else:
                                            capability = Capability(capability_id=capability_id, capabilities=vehicle.capabilities)
                                            vehicle.capabilities.add_capability(capability_id, capability)
                                        if 'status' in capability_dict and capability_dict['status'] is not None:
                                            statuses = capability_dict['status']
                                            if isinstance(statuses, list):
                                                for status in statuses:
                                                    if status in [item.value for item in Capability.Status]:
                                                        capability.status.value.append(Capability.Status(status))
                                                    else:
                                                        LOG_API.warning('Capability status unkown %s', status)
                                                        capability.status.value.append(Capability.Status.UNKNOWN)
                                            else:
                                                LOG_API.warning('Capability status not a list in %s', statuses)
                                        else:
                                            capability.status.value.clear()                                  
                                        if 'expirationDate' in capability_dict and capability_dict['expirationDate'] is not None \
                                                and capability_dict['expirationDate'] != '':
                                            expiration_date: datetime = robust_time_parse(capability_dict['expirationDate'])
                                            capability.expiration_date._set_value(expiration_date)  # pylint: disable=protected-access
                                        else:
                                            capability.expiration_date._set_value(None)  # pylint: disable=protected-access
                                        if 'editable' in capability_dict and capability_dict['editable'] is not None:
                                            # pylint: disable-next=protected-access
                                            capability.editable._set_value(capability_dict['editable'])
                                        else:
                                            capability.editable._set_value(None)  # pylint: disable=protected-access
                                        if 'parameters' in capability_dict and capability_dict['parameters'] is not None:
                                            for parameter, value in capability_dict['parameters'].items():
                                                capability.parameters[parameter] = value
                                    else:
                                        raise APIError('Could not fetch capabilities, capability ID missing')
                                    log_extra_keys(LOG_API, 'capability', capability_dict,  {'id', 'expirationDate', 'editable', 'parameters', 'status'})

                                for capability_id in vehicle.capabilities.capabilities.keys() - found_capabilities:
                                    vehicle.capabilities.remove_capability(capability_id)

                                if vehicle.capabilities.has_capability('charging', check_status_ok=True):
                                    if not isinstance(vehicle, SeatCupraElectricVehicle):
                                        LOG.debug('Promoting %s to SeatCupraElectricVehicle object for %s', vehicle.__class__.__name__, vin)
                                        vehicle = SeatCupraElectricVehicle(garage=self.car_connectivity.garage, origin=vehicle)
                                        self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                                    if not vehicle.charging.commands.contains_command('start-stop'):
                                        charging_start_stop_command: ChargingStartStopCommand = ChargingStartStopCommand(parent=vehicle.charging.commands)
                                        charging_start_stop_command._add_on_set_hook(self.__on_charging_start_stop)  # pylint: disable=protected-access
                                        charging_start_stop_command.enabled = True
                                        vehicle.charging.commands.add_command(charging_start_stop_command)

                                if vehicle.capabilities.has_capability('climatisation', check_status_ok=True):
                                    if vehicle.climatization is not None and vehicle.climatization.commands is not None \
                                            and not vehicle.climatization.commands.contains_command('start-stop'):
                                        climatisation_start_stop_command: ClimatizationStartStopCommand = \
                                            ClimatizationStartStopCommand(parent=vehicle.climatization.commands)
                                        # pylint: disable-next=protected-access
                                        climatisation_start_stop_command._add_on_set_hook(self.__on_air_conditioning_start_stop)
                                        climatisation_start_stop_command.enabled = True
                                        vehicle.climatization.commands.add_command(climatisation_start_stop_command)

                                if vehicle.capabilities.has_capability('vehicleWakeUpTrigger', check_status_ok=True):
                                    if vehicle.commands is not None and vehicle.commands.commands is not None \
                                            and not vehicle.commands.contains_command('wake-sleep'):
                                        wake_sleep_command = WakeSleepCommand(parent=vehicle.commands)
                                        wake_sleep_command._add_on_set_hook(self.__on_wake_sleep)  # pylint: disable=protected-access
                                        wake_sleep_command.enabled = True
                                        vehicle.commands.add_command(wake_sleep_command)

                                # Add honkAndFlash command if necessary capabilities are available
                                if vehicle.capabilities.has_capability('honkAndFlash', check_status_ok=True):
                                    if vehicle.commands is not None and vehicle.commands.commands is not None \
                                            and not vehicle.commands.contains_command('honk-flash'):
                                        honk_flash_command = HonkAndFlashCommand(parent=vehicle.commands, with_duration=True)
                                        honk_flash_command._add_on_set_hook(self.__on_honk_flash)  # pylint: disable=protected-access
                                        honk_flash_command.enabled = True
                                        vehicle.commands.add_command(honk_flash_command)

                                # Add lock and unlock command
                                if vehicle.capabilities.has_capability('access', check_status_ok=True):
                                    if vehicle.doors is not None and vehicle.doors.commands is not None and vehicle.doors.commands.commands is not None \
                                            and not vehicle.doors.commands.contains_command('lock-unlock'):
                                        lock_unlock_command = LockUnlockCommand(parent=vehicle.doors.commands)
                                        lock_unlock_command._add_on_set_hook(self.__on_lock_unlock)  # pylint: disable=protected-access
                                        lock_unlock_command.enabled = True
                                        vehicle.doors.commands.add_command(lock_unlock_command)
                                    else:
                                        vehicle.capabilities.clear_capabilities()
                        if isinstance(vehicle, SeatCupraVehicle):
                            vehicle = self.fetch_image(vehicle)
                    else:
                        raise APIError('Could not fetch vehicle data, VIN missing')
        for vin in set(garage.list_vehicle_vins()) - seen_vehicle_vins:
            vehicle_to_remove = garage.get_vehicle(vin)
            if vehicle_to_remove is not None and vehicle_to_remove.is_managed_by_connector(self):
                garage.remove_vehicle(vin)
        self.update_vehicles()

    def fetch_vehicle_status(self, vehicle: SeatCupraVehicle, no_cache: bool = False) -> SeatCupraVehicle:
        """
        Fetches the status of a vehicle from seat/cupra API.

        Args:
            vehicle (GenericVehicle): The vehicle object containing the VIN.

        Returns:
            None
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')

        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/vehicles/{vin}/connection'
        vehicle_connection_data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        if vehicle_connection_data is not None:
            if 'connection' in vehicle_connection_data and vehicle_connection_data['connection'] is not None \
                    and 'mode' in vehicle_connection_data['connection'] and vehicle_connection_data['connection']['mode'] is not None:
                if vehicle_connection_data['connection']['mode'] in [item.value for item in GenericVehicle.ConnectionState]:
                    connection_state: GenericVehicle.ConnectionState = GenericVehicle.ConnectionState(vehicle_connection_data['connection']['mode'])
                    vehicle.connection_state._set_value(connection_state)  # pylint: disable=protected-access
                else:
                    vehicle.connection_state._set_value(GenericVehicle.ConnectionState.UNKNOWN)  # pylint: disable=protected-access
                    LOG_API.info('Unknown connection state %s', vehicle_connection_data['connection']['mode'])
                log_extra_keys(LOG_API, f'/api/v2/vehicles/{vin}/connection', vehicle_connection_data['connection'], {'mode'})
            else:
                vehicle.connection_state._set_value(None)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, f'/api/v2/vehicles/{vin}/connection', vehicle_connection_data, {'connection'})
        else:
            vehicle.connection_state._set_value(None)  # pylint: disable=protected-access

        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v2/vehicles/{vin}/status'
        vehicle_status_data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        if vehicle_status_data:
            if 'updatedAt' in vehicle_status_data and vehicle_status_data['updatedAt'] is not None:
                captured_at: Optional[datetime] = robust_time_parse(vehicle_status_data['updatedAt'])
            else:
                captured_at: Optional[datetime] = None
            if 'locked' in vehicle_status_data and vehicle_status_data['locked'] is not None:
                if vehicle_status_data['locked']:
                    vehicle.doors.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
                else:
                    vehicle.doors.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
            if 'lights' in vehicle_status_data and vehicle_status_data['lights'] is not None:
                if vehicle_status_data['lights'] == 'on':
                    vehicle.lights.light_state._set_value(Lights.LightState.ON, measured=captured_at)  # pylint: disable=protected-access
                elif vehicle_status_data['lights'] == 'off':
                    vehicle.lights.light_state._set_value(Lights.LightState.OFF, measured=captured_at)  # pylint: disable=protected-access
                else:
                    vehicle.lights.light_state._set_value(Lights.LightState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                    LOG_API.info('Unknown lights state %s', vehicle_status_data['lights'])
            else:
                vehicle.lights.light_state._set_value(None)  # pylint: disable=protected-access

            if 'hood' in vehicle_status_data and vehicle_status_data['hood'] is not None:
                vehicle_status_data['doors']['hood'] = vehicle_status_data['hood']
            if 'trunk' in vehicle_status_data and vehicle_status_data['trunk'] is not None:
                vehicle_status_data['doors']['trunk'] = vehicle_status_data['trunk']

            if 'doors' in vehicle_status_data and vehicle_status_data['doors'] is not None:
                all_doors_closed = True
                seen_door_ids: set[str] = set()
                for door_id, door_status in vehicle_status_data['doors'].items():
                    seen_door_ids.add(door_id)
                    if door_id in vehicle.doors.doors:
                        door: Doors.Door = vehicle.doors.doors[door_id]
                    else:
                        door = Doors.Door(door_id=door_id, doors=vehicle.doors)
                        vehicle.doors.doors[door_id] = door
                    if 'open' in door_status and door_status['open'] is not None:
                        if door_status['open'] == 'true':
                            door.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
                            all_doors_closed = False
                        elif door_status['open'] == 'false':
                            door.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            door.open_state._set_value(Doors.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                            LOG_API.info('Unknown door open state %s', door_status['open'])
                    else:
                        door.open_state._set_value(None)  # pylint: disable=protected-access
                    if 'locked' in door_status and door_status['locked'] is not None:
                        if door_status['locked'] == 'true':
                            door.lock_state._set_value(Doors.LockState.LOCKED, measured=captured_at)  # pylint: disable=protected-access
                        elif door_status['locked'] == 'false':
                            door.lock_state._set_value(Doors.LockState.UNLOCKED, measured=captured_at)  # pylint: disable=protected-access
                        else:
                            door.lock_state._set_value(Doors.LockState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                            LOG_API.info('Unknown door lock state %s', door_status['locked'])
                    else:
                        door.lock_state._set_value(None)  # pylint: disable=protected-access
                    log_extra_keys(LOG_API, 'door', door_status, {'open', 'locked'})
                for door_id in vehicle.doors.doors.keys() - seen_door_ids:
                    vehicle.doors.doors[door_id].enabled = False
                if all_doors_closed:
                    vehicle.doors.open_state._set_value(Doors.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                else:
                    vehicle.doors.open_state._set_value(Doors.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
            seen_window_ids: set[str] = set()
            if 'sunRoof' in vehicle_status_data and vehicle_status_data['sunRoof'] is not None \
                    and 'windows' in vehicle_status_data and vehicle_status_data['windows'] is not None:
                vehicle_status_data['windows']['sunRoof'] = vehicle_status_data['sunRoof']

            if 'windows' in vehicle_status_data and vehicle_status_data['windows'] is not None:
                all_windows_closed = True
                for window_id, window_status in vehicle_status_data['windows'].items():
                    seen_window_ids.add(window_id)
                    if window_id in vehicle.windows.windows:
                        window: Windows.Window = vehicle.windows.windows[window_id]
                    else:
                        window = Windows.Window(window_id=window_id, windows=vehicle.windows)
                        vehicle.windows.windows[window_id] = window
                    if window_status in [item.value for item in Windows.OpenState]:
                        open_state: Windows.OpenState = Windows.OpenState(window_status)
                        if open_state == Windows.OpenState.OPEN:
                            all_windows_closed = False
                        window.open_state._set_value(open_state, measured=captured_at)  # pylint: disable=protected-access
                    else:
                        LOG_API.info('Unknown window status %s', window_status)
                        window.open_state._set_value(Windows.OpenState.UNKNOWN, measured=captured_at)  # pylint: disable=protected-access
                if all_windows_closed:
                    vehicle.windows.open_state._set_value(Windows.OpenState.CLOSED, measured=captured_at)  # pylint: disable=protected-access
                else:
                    vehicle.windows.open_state._set_value(Windows.OpenState.OPEN, measured=captured_at)  # pylint: disable=protected-access
            else:
                vehicle.windows.open_state._set_value(None)  # pylint: disable=protected-access
            for window_id in vehicle.windows.windows.keys() - seen_window_ids:
                vehicle.windows.windows[window_id].enabled = False
            log_extra_keys(LOG_API, f'/api/v2/vehicle-status/{vin}', vehicle_status_data, {'updatedAt', 'locked', 'lights', 'hood', 'trunk', 'doors',
                                                                                           'windows', 'sunRoof'})
        return vehicle

    def fetch_vehicle_mycar_status(self, vehicle: SeatCupraVehicle, no_cache: bool = False) -> SeatCupraVehicle:
        """
        Fetches the status of a vehicle from seat/cupra API.

        Args:
            vehicle (GenericVehicle): The vehicle object containing the VIN.

        Returns:
            None
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        # url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/measurements/engines'
        # vehicle_status_data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        # measurements
        # {'primary': {'fuelType': 'gasoline', 'rangeInKm': 120.0}, 'secondary': {'fuelType': 'electric', 'rangeInKm': 40.0}}
        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v5/users/{self.session.user_id}/vehicles/{vin}/mycar'
        vehicle_status_data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        if vehicle_status_data:
            if 'engines' in vehicle_status_data and vehicle_status_data['engines'] is not None:
                drive_ids: set[str] = {'primary', 'secondary'}
                total_range: float = 0.0
                for drive_id in drive_ids:
                    if drive_id in vehicle_status_data['engines'] and vehicle_status_data['engines'][drive_id] is not None \
                            and 'fuelType' in vehicle_status_data['engines'][drive_id] and vehicle_status_data['engines'][drive_id]['fuelType'] is not None:
                        try:
                            engine_type: GenericDrive.Type = GenericDrive.Type(vehicle_status_data['engines'][drive_id]['fuelType'])
                        except ValueError:
                            LOG_API.warning('Unknown fuelType type %s', vehicle_status_data['engines'][drive_id]['fuelType'])
                            engine_type: GenericDrive.Type = GenericDrive.Type.UNKNOWN

                        if drive_id in vehicle.drives.drives:
                            drive: GenericDrive = vehicle.drives.drives[drive_id]
                        else:
                            if engine_type == GenericDrive.Type.ELECTRIC:
                                drive = ElectricDrive(drive_id=drive_id, drives=vehicle.drives)
                            elif engine_type == GenericDrive.Type.DIESEL:
                                drive = DieselDrive(drive_id=drive_id, drives=vehicle.drives)
                            elif engine_type in [GenericDrive.Type.FUEL,
                                                 GenericDrive.Type.GASOLINE,
                                                 GenericDrive.Type.PETROL,
                                                 GenericDrive.Type.DIESEL,
                                                 GenericDrive.Type.CNG,
                                                 GenericDrive.Type.LPG]:
                                drive = CombustionDrive(drive_id=drive_id, drives=vehicle.drives)
                            else:
                                drive = GenericDrive(drive_id=drive_id, drives=vehicle.drives)
                            drive.type._set_value(engine_type)  # pylint: disable=protected-access
                            vehicle.drives.add_drive(drive)
                        if 'levelPct' in vehicle_status_data['engines'][drive_id] and vehicle_status_data['engines'][drive_id]['levelPct'] is not None:
                            # pylint: disable-next=protected-access
                            drive.level._set_value(value=vehicle_status_data['engines'][drive_id]['levelPct'])
                        else:
                            drive.level._set_value(None)  # pylint: disable=protected-access
                        if 'rangeKm' in vehicle_status_data['engines'][drive_id] and vehicle_status_data['engines'][drive_id]['rangeKm'] is not None:
                            # pylint: disable-next=protected-access
                            drive.range._set_value(value=vehicle_status_data['engines'][drive_id]['rangeKm'], unit=Length.KM)
                            total_range += vehicle_status_data['engines'][drive_id]['rangeKm']
                        else:
                            drive.range._set_value(None, unit=Length.KM)  # pylint: disable=protected-access
                        log_extra_keys(LOG_API, drive_id, vehicle_status_data['engines'][drive_id], {'fuelType',
                                                                                                     'levelPct',
                                                                                                     'rangeKm'})
                vehicle.drives.total_range._set_value(total_range, unit=Length.KM)  # pylint: disable=protected-access
            else:
                vehicle.drives.enabled = False
            if len(vehicle.drives.drives) > 0:
                has_electric = False
                has_combustion = False
                for drive in vehicle.drives.drives.values():
                    if isinstance(drive, ElectricDrive):
                        has_electric = True
                    elif isinstance(drive, CombustionDrive):
                        has_combustion = True
                if has_electric and not has_combustion and not isinstance(vehicle, SeatCupraElectricVehicle):
                    LOG.debug('Promoting %s to SeatCupraElectricVehicle object for %s', vehicle.__class__.__name__, vin)
                    vehicle = SeatCupraElectricVehicle(garage=self.car_connectivity.garage, origin=vehicle)
                    self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                elif has_combustion and not has_electric and not isinstance(vehicle, SeatCupraCombustionVehicle):
                    LOG.debug('Promoting %s to SeatCupraCombustionVehicle object for %s', vehicle.__class__.__name__, vin)
                    vehicle = SeatCupraCombustionVehicle(garage=self.car_connectivity.garage, origin=vehicle)
                    self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                elif has_combustion and has_electric and not isinstance(vehicle, SeatCupraHybridVehicle):
                    LOG.debug('Promoting %s to SeatCupraHybridVehicle object for %s', vehicle.__class__.__name__, vin)
                    vehicle = SeatCupraHybridVehicle(garage=self.car_connectivity.garage, origin=vehicle)
                    self.car_connectivity.garage.replace_vehicle(vin, vehicle)
            if 'services' in vehicle_status_data and vehicle_status_data['services'] is not None:
                if 'charging' in vehicle_status_data['services'] and vehicle_status_data['services']['charging'] is not None:
                    charging_status: Dict = vehicle_status_data['services']['charging']
                    if 'targetPct' in charging_status and charging_status['targetPct'] is not None:
                        if isinstance(vehicle, ElectricVehicle):
                            vehicle.charging.settings.target_level._set_value(charging_status['targetPct'])  # pylint: disable=protected-access
                    if 'chargeMode' in charging_status and charging_status['chargeMode'] is not None:
                        if charging_status['chargeMode'] in [item.value for item in Charging.ChargingType]:
                            if isinstance(vehicle, ElectricVehicle):
                                vehicle.charging.type._set_value(value=Charging.ChargingType(charging_status['chargeMode']))  # pylint: disable=protected-access
                        else:
                            LOG_API.info('Unknown charge type %s', charging_status['chargeMode'])
                            if isinstance(vehicle, ElectricVehicle):
                                vehicle.charging.type._set_value(Charging.ChargingType.UNKNOWN)  # pylint: disable=protected-access
                    else:
                        if isinstance(vehicle, ElectricVehicle):
                            vehicle.charging.type._set_value(None)  # pylint: disable=protected-access
                    if 'remainingTime' in charging_status and charging_status['remainingTime'] is not None:
                        remaining_duration: timedelta = timedelta(minutes=charging_status['remainingTime'])
                        estimated_date_reached: datetime = datetime.now(tz=timezone.utc) + remaining_duration
                        estimated_date_reached = estimated_date_reached.replace(second=0, microsecond=0)
                        if isinstance(vehicle, ElectricVehicle):
                            vehicle.charging.estimated_date_reached._set_value(value=estimated_date_reached)  # pylint: disable=protected-access
                    else:
                        if isinstance(vehicle, ElectricVehicle):
                            vehicle.charging.estimated_date_reached._set_value(None)  # pylint: disable=protected-access
                    log_extra_keys(LOG_API, 'charging', charging_status, {'status', 'targetPct', 'currentPct', 'chargeMode', 'remainingTime'})
                else:
                    if isinstance(vehicle, ElectricVehicle):
                        vehicle.charging.enabled = False
                if 'climatisation' in vehicle_status_data['services'] and vehicle_status_data['services']['climatisation'] is not None:
                    climatisation_status: Dict = vehicle_status_data['services']['climatisation']

                    if 'remainingTime' in climatisation_status and climatisation_status['remainingTime'] is not None:
                        remaining_duration: timedelta = timedelta(minutes=climatisation_status['remainingTime'])
                        estimated_date_reached: datetime = datetime.now(tz=timezone.utc) + remaining_duration
                        estimated_date_reached = estimated_date_reached.replace(second=0, microsecond=0)
                        vehicle.climatization.estimated_date_reached._set_value(value=estimated_date_reached)  # pylint: disable=protected-access
                    else:
                        vehicle.climatization.estimated_date_reached._set_value(None)  # pylint: disable=protected-access
                    # we take status, targetTemperatureCelsius, targetTemperatureFahrenheit, from climatization request
                    log_extra_keys(LOG_API, 'climatisation', climatisation_status, {'status', 'targetTemperatureCelsius', 'targetTemperatureFahrenheit',
                                                                                    'remainingTime'})
        return vehicle

    def fetch_connection_status(self, vehicle: SeatCupraVehicle, no_cache: bool = False) -> SeatCupraVehicle:
        """
        Fetches the connection status of the given Seat/Cupra vehicle and updates its connection attributes.

        Args:
            vehicle (SeatCupraVehicle): The Seat/Cupra vehicle object containing the VIN and connection attributes.

        Returns:
            SeatCupraVehicle: The updated Seat/Cupra vehicle object with the fetched connection data.

        Raises:
            APIError: If the VIN is missing.
            ValueError: If the vehicle has no connection object.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/vehicles/{vin}/connection'
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        #  {'connection': {'mode': 'online'}
        if data is not None:
            if 'connection' in data and data['connection'] is not None:
                if 'mode' in data['connection'] and data['connection']['mode'] is not None:
                    if data['connection']['mode'] in [item.value for item in GenericVehicle.ConnectionState]:
                        connection_state: GenericVehicle.ConnectionState = GenericVehicle.ConnectionState(data['connection']['mode'])
                        vehicle.connection_state._set_value(connection_state)  # pylint: disable=protected-access
                    else:
                        vehicle.connection_state._set_value(GenericVehicle.ConnectionState.UNKNOWN)  # pylint: disable=protected-access
                        LOG_API.info('Unknown connection state %s', data['connection']['mode'])
                else:
                    vehicle.connection_state._set_value(None)  # pylint: disable=protected-access
                log_extra_keys(LOG_API, 'connection status', data['connection'],  {'mode'})
            else:
                vehicle.connection_state._set_value(None)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, 'connection status', data,  {'connection'})
        return vehicle

    def fetch_parking_position(self, vehicle: SeatCupraVehicle, no_cache: bool = False) -> SeatCupraVehicle:
        """
        Fetches the position of the given vehicle and updates its position attributes.

        Args:
            vehicle (Seat/CupraVehicle): The vehicle object containing the VIN and position attributes.

        Returns:
            Seat/CupraVehicle: The updated vehicle object with the fetched position data.

        Raises:
            APIError: If the VIN is missing.
            ValueError: If the vehicle has no position object.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        if vehicle.position is None:
            raise ValueError('Vehicle has no position object')
        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/parkingposition'
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache, allow_empty=True)
        if data is not None:
            if 'lat' in data and data['lat'] is not None:
                latitude: Optional[float] = data['lat']
            else:
                latitude = None
            if 'lon' in data and data['lon'] is not None:
                longitude: Optional[float] = data['lon']
            else:
                longitude = None
            vehicle.position.latitude._set_value(latitude)  # pylint: disable=protected-access
            vehicle.position.longitude._set_value(longitude)  # pylint: disable=protected-access
            vehicle.position.position_type._set_value(Position.PositionType.PARKING)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, 'parkingposition', data,  {'lat', 'lon'})
        else:
            vehicle.position.latitude._set_value(None)  # pylint: disable=protected-access
            vehicle.position.longitude._set_value(None)  # pylint: disable=protected-access
            vehicle.position.position_type._set_value(None)  # pylint: disable=protected-access
        return vehicle

    def fetch_mileage(self, vehicle: SeatCupraVehicle, no_cache: bool = False) -> SeatCupraVehicle:
        """
        Fetches the mileage of the given vehicle and updates its mileage attributes.

        Args:
            vehicle (Seat/CupraVehicle): The vehicle object containing the VIN and mileage attributes.

        Returns:
            Seat/CupraVehicle: The updated vehicle object with the fetched mileage data.

        Raises:
            APIError: If the VIN is missing.
            ValueError: If the vehicle has no position object.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/mileage'
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        if data is not None:
            if 'mileageKm' in data and data['mileageKm'] is not None:
                vehicle.odometer._set_value(data['mileageKm'], unit=Length.KM)  # pylint: disable=protected-access
            else:
                vehicle.odometer._set_value(None)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/mileage', data,  {'mileageKm'})
        else:
            vehicle.odometer._set_value(None)  # pylint: disable=protected-access
        return vehicle

    def fetch_ranges(self, vehicle: SeatCupraVehicle, no_cache: bool = False) -> SeatCupraVehicle:
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/ranges'
        # {'ranges': [{'rangeName': 'gasolineRangeKm', 'value': 100.0}, {'rangeName': 'electricRangeKm', 'value': 28.0}]}
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        if data is not None:
            if 'ranges' in data and data['ranges'] is not None:
                for drive in vehicle.drives.drives.values():
                    if drive.type.enabled and drive.type.value == GenericDrive.Type.ELECTRIC:
                        for range_dict in data['ranges']:
                            if 'rangeName' in range_dict and range_dict['rangeName'] is not None and range_dict['rangeName'] == 'electricRangeKm' \
                                    and 'value' in range_dict and range_dict['value'] is not None:
                                drive.range._set_value(range_dict['value'], unit=Length.KM)  # pylint: disable=protected-access
                                break
                    elif drive.type.enabled and drive.type.value == GenericDrive.Type.GASOLINE:
                        for range_dict in data['ranges']:
                            if 'rangeName' in range_dict and range_dict['rangeName'] is not None and range_dict['rangeName'] == 'gasolineRangeKm' \
                                    and 'value' in range_dict and range_dict['value'] is not None:
                                drive.range._set_value(range_dict['value'], unit=Length.KM)  # pylint: disable=protected-access
                                break
                    elif drive.type.enabled and drive.type.value == GenericDrive.Type.DIESEL:
                        for range_dict in data['ranges']:
                            if 'rangeName' in range_dict and range_dict['rangeName'] is not None and range_dict['rangeName'] == 'dieselRangeKm' \
                                    and 'value' in range_dict and range_dict['value'] is not None:
                                drive.range._set_value(range_dict['value'], unit=Length.KM)  # pylint: disable=protected-access
                            elif 'rangeName' in range_dict and range_dict['rangeName'] is not None and range_dict['rangeName'] == 'adBlueKm' \
                                    and 'value' in range_dict and range_dict['value'] is not None:
                                if isinstance(drive, DieselDrive):
                                    drive.adblue_range._set_value(range_dict['value'], unit=Length.KM)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/ranges', data,  {'ranges'})
        return vehicle

    def fetch_maintenance(self, vehicle: SeatCupraVehicle, no_cache: bool = False) -> SeatCupraVehicle:
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/maintenance'
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        if data is not None:
            if 'inspectionDueDays' in data and data['inspectionDueDays'] is not None:
                inspection_due: timedelta = timedelta(days=data['inspectionDueDays'])
                inspection_date: datetime = datetime.now(tz=timezone.utc) + inspection_due
                inspection_date = inspection_date.replace(hour=0, minute=0, second=0, microsecond=0)
                # pylint: disable-next=protected-access
                vehicle.maintenance.inspection_due_at._set_value(value=inspection_date)
            else:
                vehicle.maintenance.inspection_due_at._set_value(None)  # pylint: disable=protected-access
            if 'inspectionDueKm' in data and data['inspectionDueKm'] is not None:
                vehicle.maintenance.inspection_due_after._set_value(data['inspectionDueKm'], unit=Length.KM)  # pylint: disable=protected-access
            else:
                vehicle.maintenance.inspection_due_after._set_value(None)  # pylint: disable=protected-access
            if 'oilServiceDueDays' in data and data['oilServiceDueDays'] is not None:
                oil_service_due: timedelta = timedelta(days=data['oilServiceDueDays'])
                oil_service_date: datetime = datetime.now(tz=timezone.utc) + oil_service_due
                oil_service_date = oil_service_date.replace(hour=0, minute=0, second=0, microsecond=0)
                # pylint: disable-next=protected-access
                vehicle.maintenance.oil_service_due_at._set_value(value=oil_service_date)
            else:
                vehicle.maintenance.oil_service_due_at._set_value(None)  # pylint: disable=protected-access
            if 'oilServiceDueKm' in data and data['oilServiceDueKm'] is not None:
                vehicle.maintenance.oil_service_due_after._set_value(data['oilServiceDueKm'], unit=Length.KM)  # pylint: disable=protected-access
            else:
                vehicle.maintenance.oil_service_due_after._set_value(None)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, f'/v1/vehicles/{vin}/maintenance', data,  {'inspectionDueDays', 'inspectionDueKm', 'oilServiceDueDays', 'oilServiceDueKm'})
        else:
            vehicle.odometer._set_value(None)  # pylint: disable=protected-access
        return vehicle

    def fetch_climatisation(self, vehicle: SeatCupraVehicle, no_cache: bool = False) -> SeatCupraVehicle:
        """
        Fetches the mileage of the given vehicle and updates its mileage attributes.

        Args:
            vehicle (Seat/CupraVehicle): The vehicle object containing the VIN and mileage attributes.

        Returns:
            Seat/CupraVehicle: The updated vehicle object with the fetched mileage data.

        Raises:
            APIError: If the VIN is missing.
            ValueError: If the vehicle has no position object.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/climatisation/status'
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        # {'climatisationStatus': {'carCapturedTimestamp': '2025-02-18T17:24:02Z', 'climatisationState': 'off', 'climatisationTrigger': 'unsupported'}, 'windowHeatingStatus': {'carCapturedTimestamp': '2025-02-18T16:57:51Z', 'windowHeatingStatus': [{'windowLocation': 'front', 'windowHeatingState': 'off'}, {'windowLocation': 'rear', 'windowHeatingState': 'off'}]}}
        if data is not None:
            if 'climatisationStatus' in data and data['climatisationStatus'] is not None:
                climatisation_status: Dict = data['climatisationStatus']
                if 'carCapturedTimestamp' not in climatisation_status or climatisation_status['carCapturedTimestamp'] is None:
                    raise APIError('Could not fetch vehicle status, carCapturedTimestamp missing')
                captured_at: datetime = robust_time_parse(climatisation_status['carCapturedTimestamp'])
                if 'climatisationState' in climatisation_status and climatisation_status['climatisationState'] is not None:
                    if climatisation_status['climatisationState'].lower() in [item.value for item in Climatization.ClimatizationState]:
                        climatization_state: Climatization.ClimatizationState = \
                            Climatization.ClimatizationState(climatisation_status['climatisationState'].lower())
                    else:
                        LOG_API.info('Unknown climatization state %s not in %s', climatisation_status['climatisationState'],
                                     str(Climatization.ClimatizationState))
                        climatization_state = Climatization.ClimatizationState.UNKNOWN
                    vehicle.climatization.state._set_value(value=climatization_state, measured=captured_at)  # pylint: disable=protected-access
                else:
                    vehicle.climatization.state._set_value(None)  # pylint: disable=protected-access
                log_extra_keys(LOG_API, 'climatisationStatus', data['climatisationStatus'], {'carCapturedTimestamp', 'climatisationState'})
            else:
                vehicle.climatization.state._set_value(None)  # pylint: disable=protected-access
            if 'windowHeatingStatus' in data and data['windowHeatingStatus'] is not None:
                window_heating_status: Dict = data['windowHeatingStatus']
                if 'carCapturedTimestamp' not in window_heating_status or window_heating_status['carCapturedTimestamp'] is None:
                    raise APIError('Could not fetch vehicle status, carCapturedTimestamp missing')
                captured_at: datetime = robust_time_parse(window_heating_status['carCapturedTimestamp'])
                if 'windowHeatingStatus' in window_heating_status and window_heating_status['windowHeatingStatus'] is not None:
                    heating_on: bool = False
                    all_heating_invalid: bool = True
                    for window_heating in window_heating_status['windowHeatingStatus']:
                        if 'windowLocation' in window_heating and window_heating['windowLocation'] is not None:
                            window_id = window_heating['windowLocation']
                            if window_id in vehicle.window_heatings.windows:
                                window: WindowHeatings.WindowHeating = vehicle.window_heatings.windows[window_id]
                            else:
                                window = WindowHeatings.WindowHeating(window_id=window_id, window_heatings=vehicle.window_heatings)
                                vehicle.window_heatings.windows[window_id] = window
                            if 'windowHeatingState' in window_heating and window_heating['windowHeatingState'] is not None:
                                if window_heating['windowHeatingState'] in [item.value for item in WindowHeatings.HeatingState]:
                                    window_heating_state: WindowHeatings.HeatingState = WindowHeatings.HeatingState(window_heating['windowHeatingState'])
                                    if window_heating_state == WindowHeatings.HeatingState.ON:
                                        heating_on = True
                                    if window_heating_state in [WindowHeatings.HeatingState.ON,
                                                                WindowHeatings.HeatingState.OFF]:
                                        all_heating_invalid = False
                                    window.heating_state._set_value(window_heating_state, measured=captured_at)  # pylint: disable=protected-access
                                else:
                                    LOG_API.info('Unknown window heating state %s not in %s', window_heating['windowHeatingState'],
                                                    str(WindowHeatings.HeatingState))
                                    # pylint: disable-next=protected-access
                                    window.heating_state._set_value(WindowHeatings.HeatingState.UNKNOWN, measured=captured_at)
                            else:
                                window.heating_state._set_value(None, measured=captured_at)  # pylint: disable=protected-access
                        log_extra_keys(LOG_API, 'windowHeatingStatus', window_heating, {'windowLocation', 'windowHeatingState'})
                    if all_heating_invalid:
                        # pylint: disable-next=protected-access
                        vehicle.window_heatings.heating_state._set_value(WindowHeatings.HeatingState.INVALID, measured=captured_at)
                    else:
                        if heating_on:
                            # pylint: disable-next=protected-access
                            vehicle.window_heatings.heating_state._set_value(WindowHeatings.HeatingState.ON, measured=captured_at)
                        else:
                            # pylint: disable-next=protected-access
                            vehicle.window_heatings.heating_state._set_value(WindowHeatings.HeatingState.OFF, measured=captured_at)
                if vehicle.window_heatings is not None and vehicle.window_heatings.commands is not None \
                        and not vehicle.window_heatings.commands.contains_command('start-stop'):
                    start_stop_command = WindowHeatingStartStopCommand(parent=vehicle.window_heatings.commands)
                    start_stop_command._add_on_set_hook(self.__on_window_heating_start_stop)  # pylint: disable=protected-access
                    start_stop_command.enabled = True
                    vehicle.window_heatings.commands.add_command(start_stop_command)
                log_extra_keys(LOG_API, 'windowHeatingStatus', window_heating_status, {'carCapturedTimestamp', 'windowHeatingStatus'})
            log_extra_keys(LOG_API, 'climatisation', data, {'climatisationStatus', 'windowHeatingStatus'})
        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v2/vehicles/{vin}/climatisation/settings'
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
        if data is not None:
            if not isinstance(vehicle.climatization, SeatCupraClimatization):
                vehicle.climatization = SeatCupraClimatization(vehicle=vehicle, origin=vehicle.climatization)
            if 'carCapturedTimestamp' not in data or data['carCapturedTimestamp'] is None:
                raise APIError('Could not fetch vehicle status, carCapturedTimestamp missing')
            captured_at: datetime = robust_time_parse(data['carCapturedTimestamp'])
            if 'targetTemperatureInCelsius' in data and data['targetTemperatureInCelsius'] is not None:
                # pylint: disable-next=protected-access
                vehicle.climatization.settings.target_temperature._add_on_set_hook(self.__on_air_conditioning_settings_change)
                vehicle.climatization.settings.target_temperature._is_changeable = True  # pylint: disable=protected-access

                target_temperature: Optional[float] = data['targetTemperatureInCelsius']
                vehicle.climatization.settings.target_temperature._set_value(value=target_temperature,  # pylint: disable=protected-access
                                                                             measured=captured_at,
                                                                             unit=Temperature.C)
                vehicle.climatization.settings.target_temperature.precision = 0.5
                vehicle.climatization.settings.target_temperature.minimum = 16.0
                vehicle.climatization.settings.target_temperature.maximum = 29.5
            elif 'targetTemperatureInFahrenheit' in data and data['targetTemperatureInFahrenheit'] is not None:
                # pylint: disable-next=protected-access
                vehicle.climatization.settings.target_temperature._add_on_set_hook(self.__on_air_conditioning_settings_change)
                vehicle.climatization.settings.target_temperature._is_changeable = True  # pylint: disable=protected-access

                target_temperature = data['targetTemperatureInFahrenheit']
                vehicle.climatization.settings.target_temperature._set_value(value=target_temperature,  # pylint: disable=protected-access
                                                                             measured=captured_at,
                                                                             unit=Temperature.F)
                vehicle.climatization.settings.target_temperature.precision = 0.5
                vehicle.climatization.settings.target_temperature.minimum = 61.0
                vehicle.climatization.settings.target_temperature.maximum = 85.5
            else:
                vehicle.climatization.settings.target_temperature._set_value(None)  # pylint: disable=protected-access
            if 'climatisationWithoutExternalPower' in data and data['climatisationWithoutExternalPower'] is not None:
                # pylint: disable-next=protected-access
                vehicle.climatization.settings.climatization_without_external_power._add_on_set_hook(self.__on_air_conditioning_settings_change)
                vehicle.climatization.settings.climatization_without_external_power._is_changeable = True  # pylint: disable=protected-access

                # pylint: disable-next=protected-access
                vehicle.climatization.settings.climatization_without_external_power._set_value(data['climatisationWithoutExternalPower'],
                                                                                               measured=captured_at)
            else:
                vehicle.climatization.settings.climatization_without_external_power._set_value(None)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, f'https://ola.prod.code.seat.cloud.vwgroup.com/v2/vehicles/{vin}/climatisation/settings', data,
                           {'carCapturedTimestamp', 'targetTemperatureInCelsius', 'targetTemperatureInFahrenheit', 'climatisationWithoutExternalPower'})
        return vehicle

    def fetch_charging(self, vehicle: SeatCupraVehicle, no_cache: bool = False) -> SeatCupraVehicle:
        """
        Fetches the mileage of the given vehicle and updates its mileage attributes.

        Args:
            vehicle (Seat/CupraVehicle): The vehicle object containing the VIN and mileage attributes.

        Returns:
            Seat/CupraVehicle: The updated vehicle object with the fetched mileage data.

        Raises:
            APIError: If the VIN is missing.
            ValueError: If the vehicle has no position object.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        if isinstance(vehicle, ElectricVehicle):
            url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/charging/status'
            data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)

            if data is not None:
                if 'charging' in data and data['charging'] is not None:
                    if 'state' in data['charging'] and data['charging']['state'] is not None:
                        if data['charging']['state'] in [item.value for item in SeatCupraCharging.SeatCupraChargingState]:
                            volkswagen_charging_state = SeatCupraCharging.SeatCupraChargingState(data['charging']['state'])
                            charging_state: Charging.ChargingState = mapping_seatcupra_charging_state[volkswagen_charging_state]
                        else:
                            LOG_API.info('Unkown charging state %s not in %s', data['charging']['state'],
                                         str(SeatCupraCharging.SeatCupraChargingState))
                            charging_state = Charging.ChargingState.UNKNOWN
                        vehicle.charging.state._set_value(value=charging_state)  # pylint: disable=protected-access
                    else:
                        vehicle.charging.state._set_value(None)  # pylint: disable=protected-access
                    log_extra_keys(LOG_API, 'charging',  data['charging'], {'state'})
                if 'plug' in data and data['plug'] is not None:
                    if 'connection' in data['plug'] and data['plug']['connection'] is not None:
                        if data['plug']['connection'] in [item.value for item in ChargingConnector.ChargingConnectorConnectionState]:
                            plug_state: ChargingConnector.ChargingConnectorConnectionState = \
                                ChargingConnector.ChargingConnectorConnectionState(data['plug']['connection'])
                        else:
                            LOG_API.info('Unknown plug state %s', data['plug']['connection'])
                            plug_state = ChargingConnector.ChargingConnectorConnectionState.UNKNOWN
                        vehicle.charging.connector.connection_state._set_value(value=plug_state)  # pylint: disable=protected-access
                    else:
                        vehicle.charging.connector.connection_state._set_value(value=None)  # pylint: disable=protected-access
                    if 'externalPower' in data['plug'] and data['plug']['externalPower'] is not None:
                        if data['plug']['externalPower'] in [item.value for item in ChargingConnector.ExternalPower]:
                            plug_power_state: ChargingConnector.ExternalPower = \
                                ChargingConnector.ExternalPower(data['plug']['externalPower'])
                        else:
                            if data['plug']['externalPower'] == 'ready':
                                plug_power_state = ChargingConnector.ExternalPower.AVAILABLE
                            else:
                                LOG_API.info('Unknown plug power state %s', data['plug']['externalPower'])
                                plug_power_state = ChargingConnector.ExternalPower.UNKNOWN
                        vehicle.charging.connector.external_power._set_value(value=plug_power_state)  # pylint: disable=protected-access
                    else:
                        vehicle.charging.connector.external_power._set_value(None)  # pylint: disable=protected-access
                    if 'lock' in data['plug'] and data['plug']['lock'] is not None:
                        if data['plug']['lock'] in [item.value for item in ChargingConnector.ChargingConnectorLockState]:
                            plug_lock_state: ChargingConnector.ChargingConnectorLockState = \
                                ChargingConnector.ChargingConnectorLockState(data['plug']['lock'])
                        else:
                            LOG_API.info('Unknown plug lock state %s', data['plug']['lock'])
                            plug_lock_state = ChargingConnector.ChargingConnectorLockState.UNKNOWN
                        vehicle.charging.connector.lock_state._set_value(value=plug_lock_state)  # pylint: disable=protected-access
                    else:
                        vehicle.charging.connector.lock_state._set_value(None)  # pylint: disable=protected-access
                    log_extra_keys(LOG_API, 'plug', data['plug'], {'connection', 'externalPower', 'lock'})
                log_extra_keys(LOG_API, f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/charging/status', data,
                               {'state', 'battery', 'charging', 'plug'})

            url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/charging/settings'
            data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
            if data is not None:
                if 'maxChargeCurrentAc' in data and data['maxChargeCurrentAc'] is not None:
                    if data['maxChargeCurrentAc']:
                        vehicle.charging.settings.maximum_current._set_value(value=16,  # pylint: disable=protected-access
                                                                             unit=Current.A)
                    else:
                        vehicle.charging.settings.maximum_current._set_value(value=6,  # pylint: disable=protected-access
                                                                             unit=Current.A)
                else:
                    vehicle.charging.settings.maximum_current._set_value(None)  # pylint: disable=protected-access
                if 'defaultMaxTargetSocPercentage' in data and data['defaultMaxTargetSocPercentage'] is not None:
                    vehicle.charging.settings.target_level._set_value(data['defaultMaxTargetSocPercentage'])  # pylint: disable=protected-access
                else:
                    vehicle.charging.settings.target_level._set_value(None)  # pylint: disable=protected-access
        return vehicle

    def fetch_image(self, vehicle: SeatCupraVehicle, no_cache: bool = False) -> SeatCupraVehicle:
        """
        Fetches the image of a given SeatCupraVehicle.

        This method retrieves the image of the vehicle from a remote server. It supports caching to avoid redundant downloads.
        If caching is enabled and the image is found in the cache and is not expired, it will be loaded from the cache.
        Otherwise, it will be downloaded from the server.

        Args:
            vehicle (SeatCupraVehicle): The vehicle object for which the image is to be fetched.
            no_cache (bool, optional): If True, bypasses the cache and fetches the image directly from the server. Defaults to False.

        Returns:
            SeatCupraVehicle: The vehicle object with the fetched image added to its attributes.

        Raises:
            RetrievalError: If there is a connection error, chunked encoding error, read timeout, or retry error during the image retrieval process.
        """
        if SUPPORT_IMAGES:
            url: str = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vehicle.vin.value}/renders'
            data = self._fetch_data(url, session=self.session, allow_http_error=True, no_cache=no_cache)
            if data is not None:  # pylint: disable=too-many-nested-blocks
                for image_id, image_url in data.items():
                    if image_id == 'isDefault':
                        continue
                    img = None
                    cache_date = None
                    if self.active_config['max_age'] is not None and self.session.cache is not None and image_url in self.session.cache:
                        img, cache_date_string = self.session.cache[image_url]
                        img = base64.b64decode(img)  # pyright: ignore[reportPossiblyUnboundVariable]
                        img = Image.open(io.BytesIO(img))  # pyright: ignore[reportPossiblyUnboundVariable]
                        cache_date = datetime.fromisoformat(cache_date_string)
                    if img is None or self.active_config['max_age'] is None \
                            or (cache_date is not None and cache_date < (datetime.utcnow() - timedelta(seconds=self.active_config['max_age']))):
                        try:
                            image_download_response = requests.get(image_url, stream=True, timeout=10)
                            if image_download_response.status_code == requests.codes['ok']:
                                img = Image.open(image_download_response.raw)  # pyright: ignore[reportPossiblyUnboundVariable]
                                if self.session.cache is not None:
                                    buffered = io.BytesIO()  # pyright: ignore[reportPossiblyUnboundVariable]
                                    img.save(buffered, format="PNG")
                                    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")  # pyright: ignore[reportPossiblyUnboundVariable]
                                    self.session.cache[image_url] = (img_str, str(datetime.utcnow()))
                            elif image_download_response.status_code == requests.codes['unauthorized']:
                                LOG.info('Server asks for new authorization')
                                self.session.login()
                                image_download_response = self.session.get(image_url, stream=True)
                                if image_download_response.status_code == requests.codes['ok']:
                                    img = Image.open(image_download_response.raw)  # pyright: ignore[reportPossiblyUnboundVariable]
                                    if self.session.cache is not None:
                                        buffered = io.BytesIO()  # pyright: ignore[reportPossiblyUnboundVariable]
                                        img.save(buffered, format="PNG")
                                        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")  # pyright: ignore[reportPossiblyUnboundVariable]
                                        self.session.cache[image_url] = (img_str, str(datetime.utcnow()))
                        except requests.exceptions.ConnectionError as connection_error:
                            raise RetrievalError(f'Connection error: {connection_error}') from connection_error
                        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                            raise RetrievalError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
                        except requests.exceptions.ReadTimeout as timeout_error:
                            raise RetrievalError(f'Timeout during read: {timeout_error}') from timeout_error
                        except requests.exceptions.RetryError as retry_error:
                            raise RetrievalError(f'Retrying failed: {retry_error}') from retry_error
                    if img is not None:
                        vehicle._car_images[image_id] = img  # pylint: disable=protected-access
                        if image_id == 'side':
                            if 'car_picture' in vehicle.images.images:
                                vehicle.images.images['car_picture']._set_value(img)  # pylint: disable=protected-access
                            else:
                                vehicle.images.images['car_picture'] = ImageAttribute(name="car_picture", parent=vehicle.images,
                                                                                      value=img, tags={'carconnectivity'})
        return vehicle

    def _record_elapsed(self, elapsed: timedelta) -> None:
        """
        Records the elapsed time.

        Args:
            elapsed (timedelta): The elapsed time to record.
        """
        self._elapsed.append(elapsed)

    def _fetch_data(self, url, session, no_cache=False, allow_empty=False, allow_http_error=False,
                    allowed_errors=None) -> Optional[Dict[str, Any]]:  # noqa: C901
        data: Optional[Dict[str, Any]] = None
        cache_date: Optional[datetime] = None
        if not no_cache and (self.active_config['max_age'] is not None and session.cache is not None and url in session.cache):
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
                elif status_response.status_code == requests.codes['no_content'] and allow_empty:
                    data = None
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
                    raise RetrievalError(f'Could not fetch data. Status Code was: {status_response.status_code}')
            except requests.exceptions.ConnectionError as connection_error:
                raise RetrievalError(f'Connection error: {connection_error}.'
                                     ' If this happens frequently, please check if other applications communicate with the Seat/Cupra server.') from connection_error
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

    def __on_charging_start_stop(self, start_stop_command: ChargingStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if start_stop_command.parent is None or start_stop_command.parent.parent is None \
                or start_stop_command.parent.parent.parent is None or not isinstance(start_stop_command.parent.parent.parent, SeatCupraVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: SeatCupraVehicle = start_stop_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        try:
            if command_arguments['command'] == ChargingStartStopCommand.Command.START:
                url = f'https://ola.prod.code.seat.cloud.vwgroup.com/vehicles/{vin}/charging/requests/start'
                command_response: requests.Response = self.session.post(url, allow_redirects=True)
            elif command_arguments['command'] == ChargingStartStopCommand.Command.STOP:
                url = f'https://ola.prod.code.seat.cloud.vwgroup.com/vehicles/{vin}/charging/requests/stop'
                command_response: requests.Response = self.session.post(url, allow_redirects=True)
            else:
                raise CommandError(f'Unknown command {command_arguments["command"]}')

            if command_response.status_code not in [requests.codes['ok'], requests.codes['created']]:
                LOG.error('Could not start/stop charging (%s: %s)', command_response.status_code, command_response.text)
                raise CommandError(f'Could not start/stop charging ({command_response.status_code}: {command_response.text})')
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(f'Connection error: {connection_error}.'
                               ' If this happens frequently, please check if other applications communicate with the Seat/Cupra server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f'Retrying failed: {retry_error}') from retry_error
        return command_arguments

    def __on_air_conditioning_start_stop(self, start_stop_command: ClimatizationStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if start_stop_command.parent is None or start_stop_command.parent.parent is None \
                or start_stop_command.parent.parent.parent is None or not isinstance(start_stop_command.parent.parent.parent, SeatCupraVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: SeatCupraVehicle = start_stop_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        command_dict = {}
        if command_arguments['command'] == ClimatizationStartStopCommand.Command.START:
            url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v2/vehicles/{vin}/climatisation/start'
            if vehicle.climatization.settings is None:
                raise CommandError('Could not control climatisation, there are no climatisation settings for the vehicle available.')
            precision: float = 0.5
            if 'target_temperature' in command_arguments:
                # Round target temperature to nearest 0.5
                command_dict['targetTemperature'] = round(command_arguments['target_temperature'] / precision) * precision
            elif vehicle.climatization.settings.target_temperature is not None and vehicle.climatization.settings.target_temperature.enabled \
                    and vehicle.climatization.settings.target_temperature.value is not None:
                temperature_value = vehicle.climatization.settings.target_temperature.value
                if vehicle.climatization.settings.target_temperature.precision is not None:
                    precision = vehicle.climatization.settings.target_temperature.precision
                if vehicle.climatization.settings.target_temperature.unit == Temperature.C:
                    command_dict['targetTemperatureUnit'] = 'celsius'
                elif vehicle.climatization.settings.target_temperature.unit == Temperature.F:
                    command_dict['targetTemperatureUnit'] = 'farenheit'
                else:
                    command_dict['targetTemperatureUnit'] = 'celsius'
                if temperature_value is not None:
                    command_dict['targetTemperature'] = round(temperature_value / precision) * precision
            if 'target_temperature_unit' in command_arguments:
                if command_arguments['target_temperature_unit'] == Temperature.C:
                    command_dict['targetTemperatureUnit'] = 'celsius'
                elif command_arguments['target_temperature_unit'] == Temperature.F:
                    command_dict['targetTemperatureUnit'] = 'farenheit'
                else:
                    command_dict['targetTemperatureUnit'] = 'celsius'
        elif command_arguments['command'] == ClimatizationStartStopCommand.Command.STOP:
            url: str = f'https://ola.prod.code.seat.cloud.vwgroup.com/vehicles/{vin}/climatisation/requests/stop'
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        try:
            command_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
            if command_response.status_code not in [requests.codes['ok'], requests.codes['created']]:
                LOG.error('Could not start/stop air conditioning (%s: %s)', command_response.status_code, command_response.text)
                raise CommandError(f'Could not start/stop air conditioning ({command_response.status_code}: {command_response.text})')
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(f'Connection error: {connection_error}.'
                               ' If this happens frequently, please check if other applications communicate with the Seat/Cupra server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f'Retrying failed: {retry_error}') from retry_error
        return command_arguments

    def __fetchSecurityToken(self, spin: str) -> str:
        """
        Fetches the security token from the server.

        Returns:
            str: The security token.
        """
        command_dict = {'spin': spin}
        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v2/users/{self.session.user_id}/spin/verify'
        spin_verify_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
        if spin_verify_response.status_code != requests.codes['created']:
            raise AuthenticationError(f'Could not fetch security token ({spin_verify_response.status_code}: {spin_verify_response.text})')
        data = spin_verify_response.json()
        if 'securityToken' in data:
            return data['securityToken']
        raise AuthenticationError('Could not fetch security token')

    def __on_spin(self, spin_command: SpinCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        del spin_command
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        command_dict = {}
        if self.active_config['spin'] is None:
            raise CommandError('S-PIN is missing, please add S-PIN to your configuration or .netrc file')
        if 'spin' in command_arguments:
            command_dict['spin'] = command_arguments['spin']
        else:
            if self.active_config['spin'] is None or self.active_config['spin'] == '':
                raise CommandError('S-PIN is missing, please add S-PIN to your configuration or .netrc file')
            command_dict['spin'] = self.active_config['spin']
        if command_arguments['command'] == SpinCommand.Command.VERIFY:
            url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v2/users/{self.session.user_id}/spin/verify'
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        try:
            command_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
            if command_response.status_code != requests.codes['created']:
                LOG.error('Could not execute spin command (%s: %s)', command_response.status_code, command_response.text)
                raise CommandError(f'Could not execute spin command ({command_response.status_code}: {command_response.text})')
            else:
                LOG.info('Spin verify command executed successfully')
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(f'Connection error: {connection_error}.'
                               ' If this happens frequently, please check if other applications communicate with the Seat/Cupra server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f'Retrying failed: {retry_error}') from retry_error
        return command_arguments

    def __on_wake_sleep(self, wake_sleep_command: WakeSleepCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if wake_sleep_command.parent is None or wake_sleep_command.parent.parent is None \
                or not isinstance(wake_sleep_command.parent.parent, GenericVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: GenericVehicle = wake_sleep_command.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        if command_arguments['command'] == WakeSleepCommand.Command.WAKE:
            url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/vehicle-wakeup/request'

            try:
                command_response: requests.Response = self.session.post(url, data='{}', allow_redirects=True)
                if command_response.status_code not in (requests.codes['ok'], requests.codes['no_content']):
                    LOG.error('Could not execute wake command (%s: %s)', command_response.status_code, command_response.text)
                    raise CommandError(f'Could not execute wake command ({command_response.status_code}: {command_response.text})')
            except requests.exceptions.ConnectionError as connection_error:
                raise CommandError(f'Connection error: {connection_error}.'
                                   ' If this happens frequently, please check if other applications communicate with the Seat/Cupra server.') from connection_error
            except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
            except requests.exceptions.ReadTimeout as timeout_error:
                raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
            except requests.exceptions.RetryError as retry_error:
                raise CommandError(f'Retrying failed: {retry_error}') from retry_error
        elif command_arguments['command'] == WakeSleepCommand.Command.SLEEP:
            raise CommandError('Sleep command not supported by vehicle. Vehicle will put itself to sleep')
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        return command_arguments

    def __on_honk_flash(self, honk_flash_command: HonkAndFlashCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if honk_flash_command.parent is None or honk_flash_command.parent.parent is None \
                or not isinstance(honk_flash_command.parent.parent, GenericVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: GenericVehicle = honk_flash_command.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        command_dict = {}
        if command_arguments['command'] in [HonkAndFlashCommand.Command.FLASH, HonkAndFlashCommand.Command.HONK_AND_FLASH]:
            if 'duration' in command_arguments:
                command_dict['durationInSeconds'] = command_arguments['duration']
            else:
                command_dict['durationInSeconds'] = 10
            command_dict['mode'] = command_arguments['command'].value
            command_dict['userPosition'] = {}
            if vehicle.position is None or vehicle.position.latitude is None or vehicle.position.longitude is None \
                    or vehicle.position.latitude.value is None or vehicle.position.longitude.value is None \
                    or not vehicle.position.latitude.enabled or not vehicle.position.longitude.enabled:
                raise CommandError('Can only execute honk and flash commands if vehicle position is known')
            command_dict['userPosition']['latitude'] = vehicle.position.latitude.value
            command_dict['userPosition']['longitude'] = vehicle.position.longitude.value

            url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/honk-and-flash'
            try:
                command_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True)
                if command_response.status_code not in (requests.codes['ok'], requests.codes['no_content']):
                    LOG.error('Could not execute honk or flash command (%s: %s)', command_response.status_code, command_response.text)
                    raise CommandError(f'Could not execute honk or flash command ({command_response.status_code}: {command_response.text})')
            except requests.exceptions.ConnectionError as connection_error:
                raise CommandError(f'Connection error: {connection_error}.'
                                   ' If this happens frequently, please check if other applications communicate with the Seat/Cupra server.') from connection_error
            except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
                raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
            except requests.exceptions.ReadTimeout as timeout_error:
                raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
            except requests.exceptions.RetryError as retry_error:
                raise CommandError(f'Retrying failed: {retry_error}') from retry_error
            else:
                raise CommandError(f'Unknown command {command_arguments["command"]}')
        return command_arguments

    def __on_lock_unlock(self, lock_unlock_command: LockUnlockCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if lock_unlock_command.parent is None or lock_unlock_command.parent.parent is None \
                or lock_unlock_command.parent.parent.parent is None or not isinstance(lock_unlock_command.parent.parent.parent, GenericVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise SetterError('Command arguments are not a dictionary')
        vehicle: GenericVehicle = lock_unlock_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        command_dict = {}
        if 'spin' in command_arguments:
            spin = command_arguments['spin']
        else:
            if self.active_config['spin'] is None:
                raise CommandError('S-PIN is missing, please add S-PIN to your configuration or .netrc file')
            spin = self.active_config['spin']
        sec_token = self.__fetchSecurityToken(spin)
        if command_arguments['command'] == LockUnlockCommand.Command.LOCK:
            url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/access/lock'
        elif command_arguments['command'] == LockUnlockCommand.Command.UNLOCK:
            url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/access/unlock'
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')
        try:
            headers = self.session.headers.copy()
            headers['SecToken'] = sec_token
            command_response: requests.Response = self.session.post(url, data=json.dumps(command_dict), allow_redirects=True, headers=headers)
            if command_response.status_code != requests.codes['ok']:
                LOG.error('Could not execute locking command (%s: %s)', command_response.status_code, command_response.text)
                raise CommandError(f'Could not execute locking command ({command_response.status_code}: {command_response.text})')
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(f'Connection error: {connection_error}.'
                               ' If this happens frequently, please check if other applications communicate with the Seat/Cupra server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f'Retrying failed: {retry_error}') from retry_error
        except AuthenticationError as auth_error:
            raise CommandError(f'Authentication error: {auth_error}') from auth_error
        return command_arguments

    def __on_air_conditioning_settings_change(self, attribute: GenericAttribute, value: Any) -> Any:
        """
        Callback for the climatization setting change.
        """
        if attribute.parent is None or not isinstance(attribute.parent, SeatCupraClimatization.Settings) \
                or attribute.parent.parent is None \
                or attribute.parent.parent.parent is None or not isinstance(attribute.parent.parent.parent, SeatCupraVehicle):
            raise SetterError('Object hierarchy is not as expected')
        settings: SeatCupraClimatization.Settings = attribute.parent
        vehicle: SeatCupraVehicle = attribute.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise SetterError('VIN in object hierarchy missing')
        setting_dict = {}
        if settings.target_temperature.enabled and settings.target_temperature.value is not None:
            # Round target temperature to nearest 0.5
            # Check if the attribute changed is the target_temperature attribute
            precision: float = settings.target_temperature.precision if settings.target_temperature.precision is not None else 0.5
            if isinstance(attribute, TemperatureAttribute) and attribute.id == 'target_temperature':
                value = round(value / settings.target_temperature.precision) * settings.target_temperature.precision
                setting_dict['targetTemperature'] = value
            else:
                setting_dict['targetTemperature'] = round(settings.target_temperature.value / precision) * precision
            if settings.target_temperature.unit == Temperature.C:
                setting_dict['targetTemperatureUnit'] = 'celsius'
            elif settings.target_temperature.unit == Temperature.F:
                setting_dict['targetTemperatureUnit'] = 'farenheit'
            else:
                setting_dict['targetTemperatureUnit'] = 'celsius'
        if isinstance(attribute, BooleanAttribute) and attribute.id == 'climatisation_without_external_power':
            setting_dict['climatisationWithoutExternalPower'] = value
        elif settings.climatization_without_external_power.enabled and settings.climatization_without_external_power.value is not None:
            setting_dict['climatisationWithoutExternalPower'] = settings.climatization_without_external_power.value

        url: str = f'https://ola.prod.code.seat.cloud.vwgroup.com/v2/vehicles/{vin}/climatisation/settings'
        try:
            settings_response: requests.Response = self.session.post(url, data=json.dumps(setting_dict), allow_redirects=True)
            if settings_response.status_code not in [requests.codes['ok'], requests.codes['created']]:
                LOG.error('Could not set climatization settings (%s) %s', settings_response.status_code, settings_response.text)
                raise SetterError(f'Could not set value ({settings_response.status_code}): {settings_response.text}')
        except requests.exceptions.ConnectionError as connection_error:
            raise SetterError(f'Connection error: {connection_error}.'
                              ' If this happens frequently, please check if other applications communicate with the Seat/Cupra server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise SetterError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise SetterError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise SetterError(f'Retrying failed: {retry_error}') from retry_error
        return value

    def __on_window_heating_start_stop(self, start_stop_command: WindowHeatingStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if start_stop_command.parent is None or start_stop_command.parent.parent is None \
                or start_stop_command.parent.parent.parent is None or not isinstance(start_stop_command.parent.parent.parent, SeatCupraVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: SeatCupraVehicle = start_stop_command.parent.parent.parent
        vin: Optional[str] = vehicle.vin.value
        if vin is None:
            raise CommandError('VIN in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        try:
            if command_arguments['command'] == WindowHeatingStartStopCommand.Command.START:
                url = f'https://ola.prod.code.seat.cloud.vwgroup.com/vehicles/{vin}/windowheating/requests/start'
                command_response: requests.Response = self.session.post(url, allow_redirects=True)
            elif command_arguments['command'] == WindowHeatingStartStopCommand.Command.STOP:
                url = f'https://ola.prod.code.seat.cloud.vwgroup.com/vehicles/{vin}/windowheating/requests/stop'
                command_response: requests.Response = self.session.post(url, allow_redirects=True)
            else:
                raise CommandError(f'Unknown command {command_arguments["command"]}')

            if command_response.status_code not in [requests.codes['ok'], requests.codes['created']]:
                LOG.error('Could not start/stop window heating (%s: %s)', command_response.status_code, command_response.text)
                raise CommandError(f'Could not start/stop window heating ({command_response.status_code}: {command_response.text})')
        except requests.exceptions.ConnectionError as connection_error:
            raise CommandError(f'Connection error: {connection_error}.'
                               ' If this happens frequently, please check if other applications communicate with the Seat/Cupra server.') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise CommandError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise CommandError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise CommandError(f'Retrying failed: {retry_error}') from retry_error
        return command_arguments

    def get_version(self) -> str:
        return __version__

    def get_type(self) -> str:
        return "carconnectivity-connector-seatcupra"

    def get_name(self) -> str:
        return "Seat/Cupra Connector"
