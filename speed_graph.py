"""Live throughput plot using Kivy's built-in canvas."""
from kivy.graphics import Color, Line, Rectangle
from kivy.properties import ListProperty
from kivy.uix.widget import Widget
from ui_theme import theme


class SpeedGraph(Widget):
    samples = ListProperty([])

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bind(pos=self.redraw, size=self.redraw, samples=self.redraw)
        theme.bind(colors=self.redraw)

    def redraw(self, *args):
        self.canvas.clear()
        with self.canvas:
            Color(*theme.colors["panel"])
            Rectangle(pos=self.pos, size=self.size)
            Color(*theme.colors["muted"])
            Line(points=[self.x + 12, self.top - 12, self.x + 12,
                         self.y + 12, self.right - 12, self.y + 12], width=0.5)
            if self.samples:
                xmax = max(self.samples[-1][0], 1e-9)
                ymax = max(max(s[1] for s in self.samples) * 1.1, 1)
                points = []
                for elapsed, speed in self.samples:
                    points.extend([self.x + 12 + elapsed / xmax * max(self.width - 24, 1),
                                   self.y + 12 + speed / ymax * max(self.height - 24, 1)])
                Color(*theme.colors["blue"])
                if len(points) >= 4:
                    Line(points=points, width=0.7)
                else:
                    Line(circle=(points[0], points[1], 2), width=0.5)
