# Compatibility shim: Pillow 10+ removed Image.ANTIALIAS, but moviepy 1.0.3 still
# references it during clip resizing. Map it to the modern LANCZOS filter so the
# render pipeline works on both old (Pillow 9, dev PC) and new (Pillow 10+, VPS).
try:
    from PIL import Image as _PILImage
    if not hasattr(_PILImage, "ANTIALIAS"):
        _PILImage.ANTIALIAS = _PILImage.Resampling.LANCZOS
except Exception:
    pass
