"""Gera o ícone do aplicativo em PNG e ICO."""

from pathlib import Path

from PIL import Image, ImageDraw


ASSETS = Path(__file__).resolve().parent / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

SIZE = 512
SCALE = 2
canvas = Image.new("RGBA", (SIZE * SCALE, SIZE * SCALE), (0, 0, 0, 0))
draw = ImageDraw.Draw(canvas)

draw.rounded_rectangle((16, 16, 1008, 1008), radius=230, fill="#121B30")
draw.rounded_rectangle((33, 33, 991, 991), radius=215, outline="#465877", width=5)

# Um arco representa a interface local; os três pontos, a sessão remota.
draw.arc((201, 199, 824, 823), start=47, end=312, fill="#A99AFF", width=83)
draw.arc((223, 221, 802, 801), start=52, end=303, fill="#D1C9FF", width=10)
draw.ellipse((570, 425, 708, 563), fill="#51D9BA")
draw.ellipse((616, 468, 662, 514), fill="#ECFFF9")
draw.line((698, 485, 784, 407), fill="#51D9BA", width=20)
draw.line((698, 502, 792, 591), fill="#51D9BA", width=20)
draw.ellipse((766, 372, 822, 428), fill="#51D9BA")
draw.ellipse((774, 574, 830, 630), fill="#51D9BA")

canvas = canvas.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
canvas.save(ASSETS / "comfy-colab.png")
canvas.save(
    ASSETS / "comfy-colab.ico",
    format="ICO",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
