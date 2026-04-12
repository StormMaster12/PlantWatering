from typing import Optional, TypedDict


class Plant(TypedDict):
    name:           str
    sensor_channel: int
    relay_pin:      Optional[int]
    threshold:      float         # moisture % below which we water
    water_duration: float         # seconds to run pump per cycle
