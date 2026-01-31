"""Module containing the Seat Cupra location service."""
# pylint: disable=duplicate-code
from __future__ import annotations
from typing import TYPE_CHECKING

import json
import requests

from carconnectivity_services.base.service import ServiceType
from carconnectivity_services.location.location_service import LocationService
from carconnectivity.charging_station import ChargingStation

if TYPE_CHECKING:
    from typing import Optional

    import logging

    from carconnectivity.carconnectivity import CarConnectivity
    from carconnectivity_connectors.skoda.connector import Connector


class SeatCupraLocationService(LocationService):  # pylint: disable=too-few-public-methods, too-many-instance-attributes
    """
    Service for retrieving charging station information for Seat/Cupra vehicles.
    This service provides functionality to find charging stations based on geographic coordinates
    and retrieve detailed information about them, including location, availability, power capacity,
    and operator details.
    Attributes:
        connector (Connector): The connector instance used for making API requests to Seat/Cupra services.
    """
    def __init__(self, service_id: str, car_connectivity: CarConnectivity, log: logging.Logger, connector: Connector) -> None:
        super().__init__(service_id, car_connectivity, log)
        self.connector: Connector = connector

    def get_types(self) -> list[tuple[ServiceType, int]]:
        return [(ServiceType.LOCATION_CHARGING_STATION, 100)]

    def charging_station_from_lat_lon(self, latitude: float, longitude: float, radius: int,  # pylint: disable=too-many-branches,too-many-statements
                                      charging_station: Optional[ChargingStation] = None) -> Optional[ChargingStation]:
        """
        Retrieves the closest charging station from the given latitude and longitude within a specified radius.
        Args:
            latitude (float): The latitude of the location to search for charging stations.
            longitude (float): The longitude of the location to search for charging stations.
            radius (int): The radius (in meters) within which to search for charging stations.
            charging_station (Optional[ChargingStation], optional): An optional ChargingStation object to update with the closest station's details. Defaults to None.
        Returns:
            Optional[ChargingStation]: The closest ChargingStation object with updated details if found, otherwise None.
        """
        url: str = 'https://ola.prod.code.seat.cloud.vwgroup.com/v1/charging/points'
        data = {
            "center": {"latitude": latitude, "longitude": longitude},
            "radius": radius,
        }
        status_response: requests.Response = self.connector.session.post(url, allow_redirects=False, data=json.dumps(data))
        try:
            data = status_response.json()
            if data is not None and 'points' in data:
                for place in data['points']:
                    if 'location' in place and 'position' in place['location'] and 'latitude' in place['location']['position'] and 'longitude' in place['location']['position']:
                        lat_diff = place['location']['position']['latitude'] - latitude
                        lon_diff = place['location']['position']['longitude'] - longitude
                        distance = (lat_diff**2 + lon_diff**2)**0.5 * 111000  # Approximate conversion to meters
                        place['distance'] = distance
                    else:
                        place['distance'] = float('inf')
                sorted_places = sorted(data['points'], key=lambda x: x['distance'])
                if sorted_places:
                    closest_place = sorted_places[0]
                    if 'id' in closest_place:
                        if charging_station is None:
                            charging_station = ChargingStation(name=str(closest_place['id']), parent=None)
                        charging_station.uid._set_value(closest_place['id'])  # pylint: disable=protected-access
                        charging_station.source._set_value('Seat/Cupra')  # pylint: disable=protected-access
                        if 'name' in closest_place:
                            charging_station.name._set_value(closest_place['name'])  # pylint: disable=protected-access
                        if 'location' in closest_place and 'position' in closest_place['location']:
                            charging_station.latitude._set_value(closest_place['location']['position']['latitude'])  # pylint: disable=protected-access
                            charging_station.longitude._set_value(closest_place['location']['position']['longitude'])  # pylint: disable=protected-access
                        if 'location' in closest_place and 'address' in closest_place['location']:
                            address = closest_place['location']['address']
                            address_parts = []
                            if 'street' in address:
                                address_parts.append(address['street'])
                            if 'houseNumber' in address:
                                address_parts[-1] += f" {address['houseNumber']}"
                            if 'zipCode' in address:
                                address_parts.append(address['zipCode'])
                            if 'city' in address:
                                address_parts.append(address['city'])
                            if 'country' in address:
                                address_parts.append(address['country'])
                            full_address = ', '.join(address_parts)
                            charging_station.address._set_value(full_address)  # pylint: disable=protected-access
                        if 'availability' in closest_place:
                            charging_info = closest_place['availability']
                            if 'totalConnectors' in charging_info:
                                charging_station.num_spots._set_value(charging_info['totalConnectors'])  # pylint: disable=protected-access
                        detail_url = f"https://ola.prod.code.seat.cloud.vwgroup.com/v1/charging/points/{closest_place['id']}?type=CHARGING_STATION"
                        detail_response: requests.Response = self.connector.session.get(detail_url, allow_redirects=False)
                        try:
                            detail_data = detail_response.json()
                            max_power: float = 0.0
                            if 'devices' in detail_data:
                                for device in detail_data['devices']:
                                    if 'chargingPoints' in device:
                                        for charging_point in device['chargingPoints']:
                                            if 'connectors' in charging_point:
                                                for connector in charging_point['connectors']:
                                                    if 'maxElectricPowerInWatts' in connector:
                                                        try:
                                                            power: float = float(connector['maxElectricPowerInWatts'])/1000
                                                            if power > max_power:
                                                                max_power = power
                                                        except (ValueError, TypeError):
                                                            self.log.debug(f"Invalid maxElectricPowerInWatts value: {connector['maxElectricPowerInWatts']}")
                            if max_power > 0.0:
                                charging_station.max_power._set_value(max_power)  # pylint: disable=protected-access
                            if 'provider' in detail_data and 'operator' in detail_data['provider']:
                                if detail_data['provider']['operator'] != 'other':
                                    charging_station.operator_id._set_value(detail_data['provider']['operator'])  # pylint: disable=protected-access
                                    charging_station.operator_name._set_value(detail_data['provider']['operator'])  # pylint: disable=protected-access
                        except requests.exceptions.JSONDecodeError as json_error:
                            self.log.error(f"Error decoding JSON response from Skoda API for charging station details: {json_error}")
                        return charging_station
        except requests.exceptions.JSONDecodeError as json_error:
            self.log.error(f"Error decoding JSON response from Skoda API: {json_error}")
            return None
        return None
