"""
Module for charging for Seat/Cupra vehicles.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from carconnectivity.climatization import Climatization
from carconnectivity.objects import GenericObject
from carconnectivity.vehicle import GenericVehicle

if TYPE_CHECKING:
    from typing import Optional, Dict


class SeatCupraClimatization(Climatization):  # pylint: disable=too-many-instance-attributes
    """
    SeatCupraClimatization class for handling Seat/Cupra vehicle climatization information.

    This class extends the Climatization class and includes an enumeration of various
    climatization states specific to Volkswagen vehicles.
    """
    def __init__(self, vehicle: Optional[GenericVehicle] | None = None, origin: Optional[Climatization] = None, initialization: Optional[Dict] = None) -> None:
        if origin is not None:
            super().__init__(vehicle=vehicle, origin=origin, initialization=initialization)
            if not isinstance(self.settings, SeatCupraClimatization.Settings):
                self.settings: Climatization.Settings = SeatCupraClimatization.Settings(parent=self, origin=origin.settings)
        else:
            super().__init__(vehicle=vehicle, initialization=initialization)
            self.settings: Climatization.Settings = SeatCupraClimatization.Settings(parent=self, origin=self.settings,
                                                                                    initialization=self.get_initialization('settings'))

    class Settings(Climatization.Settings):
        """
        This class represents the settings for a skoda car climatiation.
        """
        def __init__(self, parent: Optional[GenericObject] = None, origin: Optional[Climatization.Settings] = None,
                     initialization: Optional[Dict] = None) -> None:
            if origin is not None:
                super().__init__(parent=parent, origin=origin, initialization=initialization)
            else:
                super().__init__(parent=parent, initialization=initialization)
