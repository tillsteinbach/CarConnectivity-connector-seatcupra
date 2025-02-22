"""Module implements the connector to interact with the Seat/Cupra API."""
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
from carconnectivity.vehicle import GenericVehicle, ElectricVehicle, CombustionVehicle, HybridVehicle
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
from carconnectivity.position import Position

from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.seatcupra.auth.session_manager import SessionManager, SessionUser, Service
from carconnectivity_connectors.seatcupra.auth.my_cupra_session import MyCupraSession
from carconnectivity_connectors.seatcupra._version import __version__
from carconnectivity_connectors.seatcupra.charging import SeatCupraCharging, mapping_seatcupra_charging_state

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

        self.connected: BooleanAttribute = BooleanAttribute(name="connected", parent=self, tags={'connector_custom'})
        self.interval: DurationAttribute = DurationAttribute(name="interval", parent=self, tags={'connector_custom'})
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
        fetch: bool = True
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
                self._stop_event.wait(900)
            except RetrievalError as err:
                LOG.error('Retrieval error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            except APIError as err:
                LOG.error('API error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            except APICompatibilityError as err:
                LOG.error('API compatability error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            except TemporaryAuthenticationError as err:
                LOG.error('Temporary authentification error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self._stop_event.wait(interval)
            else:
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

    def update_vehicles(self) -> None:
        """
        Updates the status of all vehicles in the garage managed by this connector.

        This method iterates through all vehicle VINs in the garage, and for each vehicle that is
        managed by this connector and is an instance of SkodaVehicle, it updates the vehicle's status
        by fetching data from various APIs. If the vehicle is an instance of SkodaElectricVehicle,
        it also fetches charging information.

        Returns:
            None
        """
        garage: Garage = self.car_connectivity.garage
        for vin in set(garage.list_vehicle_vins()):
            vehicle_to_update: Optional[GenericVehicle] = garage.get_vehicle(vin)
            if vehicle_to_update is not None and vehicle_to_update.is_managed_by_connector(self):
                vehicle_to_update = self.fetch_vehicle_status(vehicle_to_update)
                vehicle_to_update = self.fetch_vehicle_mycar_status(vehicle_to_update)
                # TODO check for parking capability
                vehicle_to_update = self.fetch_parking_position(vehicle_to_update)

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

        seen_vehicle_vins: set[str] = set()
        if data is not None:
            if 'vehicles' in data and data['vehicles'] is not None:
                for vehicle_dict in data['vehicles']:
                    if 'vin' in vehicle_dict and vehicle_dict['vin'] is not None:
                        seen_vehicle_vins.add(vehicle_dict['vin'])
                        vehicle: Optional[GenericVehicle] = garage.get_vehicle(vehicle_dict['vin'])  # pyright: ignore[reportAssignmentType]
                        if vehicle is None:
                            vehicle = GenericVehicle(vin=vehicle_dict['vin'], garage=garage, managing_connector=self)
                            garage.add_vehicle(vehicle_dict['vin'], vehicle)

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
                            

                        #TODO:  https://ola.prod.code.seat.cloud.vwgroup.com/vehicles/{{VIN}}/connection

                        #TODO: https://ola.prod.code.seat.cloud.vwgroup.com/v2/vehicles/{{VIN}}/capabilities
                    else:
                        raise APIError('Could not fetch vehicle data, VIN missing')
        for vin in set(garage.list_vehicle_vins()) - seen_vehicle_vins:
            vehicle_to_remove = garage.get_vehicle(vin)
            if vehicle_to_remove is not None and vehicle_to_remove.is_managed_by_connector(self):
                garage.remove_vehicle(vin)
        self.update_vehicles()

    def fetch_vehicle_status(self, vehicle: GenericVehicle, no_cache: bool = False) -> GenericVehicle:
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
            if 'windows' in vehicle_status_data and vehicle_status_data['windows'] is not None:
                all_windows_closed = True
                for window_id, window_status in vehicle_status_data['windows'].items():
                    seen_window_ids.add(window_id)
                    if window_id in vehicle.windows.windows:
                        window: Windows.Window = vehicle.windows.windows[window_id]
                    else:
                        window = Windows.Window(window_id=window_id, windows=vehicle.windows)
                        vehicle.windows.windows[window_id] = window
                    if window_status in Windows.OpenState:
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
                                                                                           'windows'})
        return vehicle
    
    def fetch_vehicle_mycar_status(self, vehicle: GenericVehicle, no_cache: bool = False) -> GenericVehicle:
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
                if has_electric and not has_combustion and not isinstance(vehicle, ElectricVehicle):
                    LOG.debug('Promoting %s to ElectricVehicle object for %s', vehicle.__class__.__name__, vin)
                    vehicle = ElectricVehicle(origin=vehicle)
                    self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                elif has_combustion and not has_electric and not isinstance(vehicle, CombustionVehicle):
                    LOG.debug('Promoting %s to CombustionVehicle object for %s', vehicle.__class__.__name__, vin)
                    vehicle = CombustionVehicle(origin=vehicle)
                    self.car_connectivity.garage.replace_vehicle(vin, vehicle)
                elif has_combustion and has_electric and not isinstance(vehicle, HybridVehicle):
                    LOG.debug('Promoting %s to HybridVehicle object for %s', vehicle.__class__.__name__, vin)
                    vehicle = HybridVehicle(origin=vehicle)
                    self.car_connectivity.garage.replace_vehicle(vin, vehicle)
            if 'services' in vehicle_status_data and vehicle_status_data['services'] is not None:
                if 'charging' in vehicle_status_data['services'] and vehicle_status_data['services']['charging'] is not None:
                    charging_status: Dict = vehicle_status_data['services']['charging']
                    if 'status' in charging_status and charging_status['status'] is not None:
                        if charging_status['status'] in SeatCupraCharging.SeatCupraChargingState:
                            volkswagen_charging_state = SeatCupraCharging.SeatCupraChargingState(charging_status['status'])
                            charging_state: Charging.ChargingState = mapping_seatcupra_charging_state[volkswagen_charging_state]
                        else:
                            LOG_API.info('Unkown charging state %s not in %s', charging_status['status'],
                                         str(SeatCupraCharging.SeatCupraChargingState))
                            charging_state = Charging.ChargingState.UNKNOWN
                        if isinstance(vehicle, ElectricVehicle):
                            vehicle.charging.state._set_value(value=charging_state)  # pylint: disable=protected-access
                        else:
                            LOG_API.warning('Vehicle is not an electric or hybrid vehicle, but charging state was fetched')
                    else:
                        if isinstance(vehicle, ElectricVehicle):
                            vehicle.charging.state._set_value(None)  # pylint: disable=protected-access
                        else:
                            LOG_API.warning('Vehicle is not an electric or hybrid vehicle, but charging state was fetched')
                    if 'targetPct' in charging_status and charging_status['targetPct'] is not None:
                        if isinstance(vehicle, ElectricVehicle):
                            vehicle.charging.settings.target_level._set_value(charging_status['targetPct'])  # pylint: disable=protected-access
                    if 'chargeMode' in charging_status and charging_status['chargeMode'] is not None:
                        if charging_status['chargeMode'] in Charging.ChargingType:
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
                    if 'status' in climatisation_status and climatisation_status['status'] is not None:
                        if climatisation_status['status'].lower() in Climatization.ClimatizationState:
                            climatization_state: Climatization.ClimatizationState = Climatization.ClimatizationState(climatisation_status['status'].lower())
                        else:
                            LOG_API.info('Unknown climatization state %s not in %s', climatisation_status['status'],
                                         str(Climatization.ClimatizationState))
                            climatization_state = Climatization.ClimatizationState.UNKNOWN
                        vehicle.climatization.state._set_value(value=climatization_state)  # pylint: disable=protected-access
                    else:
                        vehicle.climatization.state._set_value(None)  # pylint: disable=protected-access
                    if 'targetTemperatureCelsius' in climatisation_status and climatisation_status['targetTemperatureCelsius'] is not None:
                        target_temperature: Optional[float] = climatisation_status['targetTemperatureCelsius']
                        vehicle.climatization.settings.target_temperature._set_value(value=target_temperature,  # pylint: disable=protected-access
                                                                                     unit=Temperature.C)
                    elif 'targetTemperatureFahrenheit' in climatisation_status and climatisation_status['targetTemperatureFahrenheit'] is not None:
                        target_temperature = climatisation_status['targetTemperatureFahrenheit']
                        vehicle.climatization.settings.target_temperature._set_value(value=target_temperature,  # pylint: disable=protected-access
                                                                                     unit=Temperature.F)
                    else:
                        vehicle.climatization.settings.target_temperature._set_value(None)  # pylint: disable=protected-access
                    if 'remainingTime' in climatisation_status and climatisation_status['remainingTime'] is not None:
                        remaining_duration: timedelta = timedelta(minutes=climatisation_status['remainingTime'])
                        estimated_date_reached: datetime = datetime.now(tz=timezone.utc) + remaining_duration
                        estimated_date_reached = estimated_date_reached.replace(second=0, microsecond=0)
                        vehicle.charging.estimated_date_reached._set_value(value=estimated_date_reached)  # pylint: disable=protected-access
                    else:
                        vehicle.charging.estimated_date_reached._set_value(None)  # pylint: disable=protected-access
                    log_extra_keys(LOG_API, 'climatisation', climatisation_status, {'status', 'targetTemperatureCelsius', 'targetTemperatureFahrenheit',
                                                                                    'remainingTime'})
        return vehicle

    def fetch_parking_position(self, vehicle: GenericVehicle, no_cache: bool = False) -> GenericVehicle:
        """
        Fetches the position of the given vehicle and updates its position attributes.

        Args:
            vehicle (SkodaVehicle): The vehicle object containing the VIN and position attributes.

        Returns:
            SkodaVehicle: The updated vehicle object with the fetched position data.

        Raises:
            APIError: If the VIN is missing.
            ValueError: If the vehicle has no position object.
        """
        vin = vehicle.vin.value
        if vin is None:
            raise APIError('VIN is missing')
        if vehicle.position is None:
            raise ValueError('Vehicle has no charging object')
        url = f'https://ola.prod.code.seat.cloud.vwgroup.com/v1/vehicles/{vin}/parkingposition'
        data: Dict[str, Any] | None = self._fetch_data(url=url, session=self.session, no_cache=no_cache)
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
                                     ' If this happens frequently, please check if other applications communicate with the Skoda server.') from connection_error
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
        return "carconnectivity-connector-seatcupra"
