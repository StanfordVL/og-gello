from joylo_controller import JoyLoController
from r1prot_controller import R1ProTController

CONTROLLER_LIB = {
    "JoyLo": JoyLoController,
    "R1ProT": R1ProTController,
}


__all__ = [
    "CONTROLLER_LIB"
]