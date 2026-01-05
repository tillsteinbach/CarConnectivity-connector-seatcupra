"""Module for vehicle classes."""
from __future__ import annotations

from typing import TYPE_CHECKING

import threading

from datetime import datetime

from carconnectivity.vehicle import GenericVehicle, ElectricVehicle, CombustionVehicle, HybridVehicle
from carconnectivity.attributes import BooleanAttribute

from carconnectivity_connectors.seatcupra.capability import Capabilities
from carconnectivity_connectors.seatcupra.climatization import SeatCupraClimatization
from carconnectivity_connectors.seatcupra.charging import SeatCupraCharging

SUPPORT_IMAGES = False
try:
    from PIL import Image
    SUPPORT_IMAGES = True
except ImportError:
    pass

if TYPE_CHECKING:
    from typing import Optional, Dict
    from carconnectivity.garage import Garage
    from carconnectivity.charging import Charging

    from carconnectivity_connectors.base.connector import BaseConnector


class SeatCupraVehicle(GenericVehicle):  # pylint: disable=too-many-instance-attributes
    """
    A class to represent a generic Seat/Cupra vehicle.

    Attributes:
    -----------
    vin : StringAttribute
        The vehicle identification number (VIN) of the vehicle.
    license_plate : StringAttribute
        The license plate of the vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SeatCupraVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
            self.capabilities: Capabilities = origin.capabilities
            self.capabilities.parent = self
            self.is_active: BooleanAttribute = origin.is_active
            self.is_active.parent = self
            self.last_measurement: Optional[datetime] = origin.last_measurement
            self.official_connection_state: Optional[GenericVehicle.ConnectionState] = origin.official_connection_state
            self.online_timeout_timer: Optional[threading.Timer] = origin.online_timeout_timer
            self.online_timeout_timer_lock: threading.Lock = threading.Lock()
            if SUPPORT_IMAGES:
                self._car_images = origin._car_images
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
            self.climatization = SeatCupraClimatization(vehicle=self, origin=self.climatization,
                                                        initialization=self.get_initialization('climatization'))
            self.capabilities: Capabilities = Capabilities(vehicle=self, initialization=self.get_initialization('capabilities'))
            self.is_active: BooleanAttribute = BooleanAttribute(name='is_active', parent=self, tags={'connector_custom'},
                                                                initialization=self.get_initialization('is_active'))
            self.last_measurement = None
            self.official_connection_state = None
            self.online_timeout_timer: Optional[threading.Timer] = None
            if SUPPORT_IMAGES:
                self._car_images: Dict[str, Image.Image] = {}

    def __del__(self) -> None:
        with self.online_timeout_timer_lock:
            if self.online_timeout_timer is not None:
                self.online_timeout_timer.cancel()
                self.online_timeout_timer = None


class SeatCupraElectricVehicle(ElectricVehicle, SeatCupraVehicle):
    """
    Represents a Seat/Cupra electric vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SeatCupraVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
            if isinstance(origin, ElectricVehicle):
                self.charging: Charging = SeatCupraCharging(vehicle=self, origin=origin.charging)
            else:
                self.charging: Charging = SeatCupraCharging(vehicle=self, origin=self.charging)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
            self.charging: Charging = SeatCupraCharging(vehicle=self, origin=self.charging, initialization=self.get_initialization('charging'))


class SeatCupraCombustionVehicle(CombustionVehicle, SeatCupraVehicle):
    """
    Represents a Seat/Cupra combustion vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SeatCupraVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)


class SeatCupraHybridVehicle(HybridVehicle, SeatCupraVehicle):
    """
    Represents a Seat/Cupra hybrid vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SeatCupraVehicle] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin, initialization=initialization)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector, initialization=initialization)
