"""Module for vehicle classes."""
from __future__ import annotations
from typing import TYPE_CHECKING

from carconnectivity.vehicle import GenericVehicle, ElectricVehicle, CombustionVehicle, HybridVehicle

from carconnectivity_connectors.seatcupra.capability import Capabilities
from carconnectivity_connectors.seatcupra.climatization import SeatCupraClimatization

SUPPORT_IMAGES = False
try:
    from PIL import Image
    SUPPORT_IMAGES = True
except ImportError:
    pass

if TYPE_CHECKING:
    from typing import Optional, Dict
    from carconnectivity.garage import Garage
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
                 origin: Optional[SeatCupraVehicle] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin)
            self.capabilities: Capabilities = origin.capabilities
            self.capabilities.parent = self
            if SUPPORT_IMAGES:
                self._car_images = origin._car_images
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
            self.climatization = SeatCupraClimatization(vehicle=self, origin=self.climatization)
            self.capabilities: Capabilities = Capabilities(vehicle=self)
            if SUPPORT_IMAGES:
                self._car_images: Dict[str, Image.Image] = {}


class SeatCupraElectricVehicle(ElectricVehicle, SeatCupraVehicle):
    """
    Represents a Seat/Cupra electric vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SeatCupraVehicle] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)


class SeatCupraCombustionVehicle(CombustionVehicle, SeatCupraVehicle):
    """
    Represents a Seat/Cupra combustion vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SeatCupraVehicle] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)


class SeatCupraHybridVehicle(HybridVehicle, SeatCupraVehicle):
    """
    Represents a Seat/Cupra hybrid vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[SeatCupraVehicle] = None) -> None:
        if origin is not None:
            super().__init__(garage=garage, origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
