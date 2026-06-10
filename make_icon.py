from PIL import Image, ImageDraw, ImageFont

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# rounded green tile so it reads well on any tab/desktop background
d.rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], radius=52, fill=(33, 160, 71, 255))

font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 200)
bbox = d.textbbox((0, 0), "$", font=font)
w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
x = (SIZE - w) / 2 - bbox[0]
y = (SIZE - h) / 2 - bbox[1]
d.text((x, y), "$", font=font, fill=(255, 255, 255, 255))

img.save(r"static\favicon.ico",
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("favicon.ico written")
