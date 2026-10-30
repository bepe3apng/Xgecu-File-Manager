"""Reference-inspired blue, silver and charcoal themes with semantic controls."""
from kivy.event import EventDispatcher
from kivy.properties import BooleanProperty, DictProperty
from kivy.utils import get_color_from_hex


PALETTES = {
    "light": dict(base="#F3F3F3", mantle="#EBEBEB", crust="#FFFFFF",
                  surface="#E0E4E7", text="#20262B", muted="#48545E",
                  subtle="#596570", primary="#115477", on_primary="#FFFFFF",
                  blue="#115477", green="#486B20", orange="#805600", init_button="#AFFC41",
                  red="#8D2626", teal="#11696E", peach="#943D19"),
    "dark": dict(base="#16191D", mantle="#20262C", crust="#191E24",
                 surface="#303941", text="#F3F3F3", muted="#C4CDD4",
                 subtle="#A8B6C2", primary="#72C5EC", on_primary="#0C335A",
                 blue="#72C5EC", green="#B4D37E", orange="#FFBE4B", init_button="#548C2F",
                 red="#FFAAA7", teal="#62D4D8", peach="#FFAB83"),
}

# Exact swatches from the reference; label colors are selected by contrast.
BUTTONS = dict(blue="#2278A5", green="#115477", teal="#11696E",
               orange="#B54720", peach="#943D19", red="#CF2F2F",
               purple="#6F268D")


def blend(first, second, amount):
    return tuple(a * amount + b * (1 - amount) for a, b in zip(first[:3], second[:3])) + (1,)


def luminance(color):
    channels = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
                for v in color[:3]]
    return sum(v * w for v, w in zip(channels, (.2126, .7152, .0722)))


class Theme(EventDispatcher):
    light = BooleanProperty(False)
    colors = DictProperty({})

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.on_light(self, self.light)

    def on_light(self, instance, light):
        self.colors = self.palette(light)

    @staticmethod
    def button_text(background):
        # Choose the higher-contrast black or white, including pressed states.
        return (0, 0, 0, 1) if luminance(background) > .179 else (1, 1, 1, 1)

    @staticmethod
    def button_background(text, colors):
        if text == "Scatter -> GPT":
            return colors["purple_button"]
        if text.startswith("Записать"):
            return colors["orange_button"]
        if text.startswith("Инит"):
            return colors["init_button"]
        if text in ("К последним", "Обновить файлы"):
            return colors["blue_button"]
        return colors["button"]

    @staticmethod
    def palette(light):
        palette = {key: tuple(get_color_from_hex(value))
                   for key, value in PALETTES["light" if light else "dark"].items()}
        colors = dict(palette)
        colors.update(background=palette["base"], panel=palette["mantle"],
                      log=palette["crust"], row=palette["base"],
                      alternate=palette["mantle"], header=palette["surface"],
                      input=palette["crust"], button=palette["surface"])
        for accent, value in BUTTONS.items():
            colors[accent + "_button"] = tuple(get_color_from_hex(value))
        return colors


theme = Theme()
