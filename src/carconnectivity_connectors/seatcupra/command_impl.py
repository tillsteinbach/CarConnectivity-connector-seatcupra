"""This module defines the classes that represent attributes in the CarConnectivity system."""
from __future__ import annotations
from typing import TYPE_CHECKING, Dict, Union

from enum import Enum
import argparse
import json
import logging
import shlex

from carconnectivity.commands import GenericCommand
from carconnectivity.objects import GenericObject
from carconnectivity.errors import SetterError
from carconnectivity.util import ThrowingArgumentParser

if TYPE_CHECKING:
    from carconnectivity.objects import Optional

LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.seatcupra")


class SpinCommand(GenericCommand):
    """
    SpinCommand is a command class for verifying the spin

    """
    def __init__(self, name: str = 'spin', parent: Optional[GenericObject] = None, initialization: Optional[Dict] = None) -> None:
        super().__init__(name=name, parent=parent, initialization=initialization)

    @property
    def value(self) -> Optional[Union[str, Dict]]:
        return super().value

    @value.setter
    def value(self, new_value: Optional[Union[str, Dict]]) -> None:
        # Execute early hooks before parsing the value
        new_value = self._execute_on_set_hook(new_value, early_hook=True)
        if isinstance(new_value, SpinCommand.Command):
            newvalue_dict = {}
            newvalue_dict['command'] = new_value
            new_value = newvalue_dict
        elif isinstance(new_value, str):
            parser = ThrowingArgumentParser(prog='', add_help=False, exit_on_error=False)
            parser.add_argument('command', help='Command to execute', type=SpinCommand.Command,
                                choices=list(SpinCommand.Command))
            parser.add_argument('--spin', dest='spin', help='Spin to be used instead of spin from config or .netrc', type=str, required=False,
                                default=None)
            try:
                args = parser.parse_args(new_value.strip().split(sep=' '))
            except argparse.ArgumentError as e:
                raise SetterError(f'Invalid format for SpinCommand: {e.message} {parser.format_usage()}') from e

            newvalue_dict = {}
            newvalue_dict['command'] = args.command
            if args.spin is not None:
                newvalue_dict['spin'] = args.spin
            new_value = newvalue_dict
        elif isinstance(new_value, dict):
            if 'command' in new_value and isinstance(new_value['command'], str):
                if new_value['command'] in SpinCommand.Command:
                    new_value['command'] = SpinCommand.Command(new_value['command'])
                else:
                    raise ValueError('Invalid value for SpinCommand. '
                                     f'Command must be one of {SpinCommand.Command}')
        if self._is_changeable:
            # Execute late hooks before setting the value
            new_value = self._execute_on_set_hook(new_value, early_hook=False)
            self._set_value(new_value)
        else:
            raise TypeError('You cannot set this attribute. Attribute is not mutable.')

    class Command(Enum):
        """
        Enum class representing different commands for SPIN.

        """
        VERIFY = 'verify'

        def __str__(self) -> str:
            return self.value


class DestinationCommand(GenericCommand):
    """
    DestinationCommand is a command class to send a navigation destination to the vehicle.

    """
    def __init__(self, name: str = 'send-destination', parent: Optional[GenericObject] = None, initialization: Optional[Dict] = None) -> None:
        super().__init__(name=name, parent=parent, initialization=initialization)

    @property
    def value(self) -> Optional[Union[str, Dict]]:
        return super().value

    @staticmethod
    def _normalize_destination(command_dict: Dict) -> Dict:
        """Normalize destination data from various input formats into the canonical API format.

        Accepts both nested format (destination.geoCoordinate.latitude) and flat keys (latitude at top level).
        """
        destination_raw = command_dict.get('destination') or {}
        if not isinstance(destination_raw, dict):
            raise SetterError('Destination must be a dictionary')

        geo = destination_raw.get('geoCoordinate') or {}
        if not isinstance(geo, dict):
            raise SetterError('destination.geoCoordinate must be a dictionary')

        # Extract and validate coordinates from nested or flat format
        raw_lat = geo.get('latitude')
        if raw_lat is None:
            raw_lat = command_dict.get('latitude')
        raw_lon = geo.get('longitude')
        if raw_lon is None:
            raw_lon = command_dict.get('longitude')
        if raw_lat is None or raw_lon is None:
            raise SetterError('Destination latitude and longitude are required')
        try:
            latitude = float(raw_lat)
            longitude = float(raw_lon)
        except (TypeError, ValueError) as e:
            raise SetterError('Destination latitude and longitude must be numeric') from e
        if not -90 <= latitude <= 90:
            raise SetterError('Destination latitude must be between -90 and 90')
        if not -180 <= longitude <= 180:
            raise SetterError('Destination longitude must be between -180 and 180')

        # POI provider (default: google)
        poi_provider = (destination_raw.get('poiProvider') or command_dict.get('poiProvider')
                        or command_dict.get('poi_provider'))
        if poi_provider is not None:
            poi_provider = str(poi_provider).strip()
        if not poi_provider:
            poi_provider = 'google'

        destination: Dict = {
            'poiProvider': poi_provider,
            'geoCoordinate': {'latitude': latitude, 'longitude': longitude},
        }

        # Optional destination name
        dest_name = (destination_raw.get('destinationName') or command_dict.get('destinationName')
                     or command_dict.get('destination_name'))
        if dest_name is not None:
            dest_name = str(dest_name).strip()
            if dest_name:
                destination['destinationName'] = dest_name

        # Optional address fields
        address_raw = destination_raw.get('address') or {}
        if not isinstance(address_raw, dict):
            raise SetterError('destination.address must be a dictionary')

        address: Dict[str, str] = {}
        for api_key, alt_key in [('street', 'street'), ('houseNumber', 'house_number'),
                                  ('city', 'city'), ('zipCode', 'zip_code'), ('country', 'country')]:
            val = address_raw.get(api_key) or command_dict.get(api_key) or command_dict.get(alt_key)
            if val is not None:
                val = str(val).strip()
                if val:
                    address[api_key] = val

        # Handle stateAbbreviation (including common typo 'stateAbbrevation')
        state = (address_raw.get('stateAbbreviation') or address_raw.get('stateAbbrevation')
                 or command_dict.get('stateAbbreviation') or command_dict.get('stateAbbrevation')
                 or command_dict.get('state_abbreviation'))
        if state is not None:
            state = str(state).strip()
            if state:
                address['stateAbbreviation'] = state

        if address:
            destination['address'] = address

        return destination

    @value.setter
    def value(self, new_value: Optional[Union[str, Dict]]) -> None:
        # Execute early hooks before parsing the value
        new_value = self._execute_on_set_hook(new_value, early_hook=True)
        if isinstance(new_value, DestinationCommand.Command):
            newvalue_dict: Dict = {}
            newvalue_dict['command'] = new_value
            new_value = newvalue_dict
        elif isinstance(new_value, str):
            stripped_value = new_value.strip()
            if stripped_value.startswith('{'):
                try:
                    parsed_dict = json.loads(stripped_value)
                except json.JSONDecodeError as err:
                    raise SetterError(f'Invalid JSON format for DestinationCommand: {err}') from err
                if not isinstance(parsed_dict, dict):
                    raise SetterError('Destination command JSON must be a dictionary')
                new_value = parsed_dict
            else:
                parser = ThrowingArgumentParser(prog='', add_help=False, exit_on_error=False)
                parser.add_argument('command', help='Command to execute', type=DestinationCommand.Command,
                                    choices=list(DestinationCommand.Command))
                parser.add_argument('--latitude', help='Latitude of destination', type=float, required=True)
                parser.add_argument('--longitude', help='Longitude of destination', type=float, required=True)
                parser.add_argument('--poi-provider', dest='poi_provider', help='POI provider', type=str, default=None)
                parser.add_argument('--destination-name', dest='destination_name', help='Destination name', type=str, default=None)
                parser.add_argument('--street', help='Street name', type=str, default=None)
                parser.add_argument('--house-number', dest='house_number', help='House number', type=str, default=None)
                parser.add_argument('--city', help='City', type=str, default=None)
                parser.add_argument('--zip-code', dest='zip_code', help='ZIP/postal code', type=str, default=None)
                parser.add_argument('--country', help='Country', type=str, default=None)
                parser.add_argument('--state-abbreviation', dest='state_abbreviation', help='State abbreviation', type=str, default=None)
                try:
                    args = parser.parse_args(shlex.split(stripped_value))
                except argparse.ArgumentError as e:
                    raise SetterError(f'Invalid format for DestinationCommand: {e.message} {parser.format_usage()}') from e
                new_value = vars(args)
        if isinstance(new_value, dict):
            if 'command' not in new_value:
                new_value['command'] = DestinationCommand.Command.SEND
            if 'command' in new_value and isinstance(new_value['command'], str):
                if new_value['command'] in DestinationCommand.Command:
                    new_value['command'] = DestinationCommand.Command(new_value['command'])
                else:
                    raise SetterError('Invalid value for DestinationCommand. '
                                      f'Command must be one of {DestinationCommand.Command}')
            if not isinstance(new_value['command'], DestinationCommand.Command):
                raise SetterError('Destination command is invalid')
            new_value = {
                'command': new_value['command'],
                'destination': DestinationCommand._normalize_destination(new_value),
            }
        if self._is_changeable:
            # Execute late hooks before setting the value
            new_value = self._execute_on_set_hook(new_value, early_hook=False)
            self._set_value(new_value)
        else:
            raise TypeError('You cannot set this attribute. Attribute is not mutable.')

    class Command(Enum):
        """
        Enum class representing different commands for destination command.

        """
        SEND = 'send'

        def __str__(self) -> str:
            return self.value
