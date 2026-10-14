"""Frame composition of a drawn illustration (shared by the renderer, the builder and the metrics):
paper <- paint (flat marker color, revealed by the moving marker) <- shade (source paint, soft broad strokes)
<- sketch (light construction lines, faded after inking) <- line layer (revealed exactly where the nib passed)."""
from pathlib import Path
import numpy as np
from PIL import Image

SHARP = 250.   # reveal ramp of a nib or marker edge, in fractions of the event


def over(top_rgb, top_alpha, rgb, alpha):
    result = top_alpha + alpha * (1 - top_alpha)
    color = (top_rgb * top_alpha[..., None] + rgb * (alpha * (1 - top_alpha))[..., None]) / np.maximum(result, 1e-6)[..., None]
    return color, result


def prepare(maps, line, paint, shade, sketch=None):
    """maps: npz-like time maps; line/paint/shade/sketch: RGBA uint8 arrays."""
    as_float = lambda image: np.asarray(image, np.float32) / 255
    line, paint, shade = as_float(line), as_float(paint), as_float(shade)
    layers = {'line_rgb': line[..., :3], 'line_alpha': line[..., 3], 'flat': paint[..., :3], 'shade_rgb': shade[..., :3],
              'alpha': shade[..., 3], 'ink': np.asarray(maps['ink'], np.float32),
              'ink_ramp': np.maximum(np.asarray(maps['ink_ramp'], np.float32), 1 / SHARP),
              'color': np.asarray(maps['color'], np.float32), 'cover': np.asarray(maps['color_cover'], np.float32),
              'full': np.asarray(maps['color_full'], np.float32), 'shade': np.asarray(maps['shade'], np.float32),
              'shade_ramp': float(maps['shade_ramp'])}
    if sketch is not None:
        sketch = as_float(sketch)
        layers.update(sketch_rgb=sketch[..., :3], sketch_alpha=sketch[..., 3], sketch=np.asarray(maps['sketch'], np.float32),
                      sketch_fade=tuple(float(value) for value in maps['sketch_fade']))
    return layers


def load(directory, name):
    directory = Path(directory)
    image = lambda suffix: np.array(Image.open(directory / f'{name}-{suffix}.png').convert('RGBA'))
    maps = np.load(directory / f'{name}.npz')
    sketch = image('sketch') if (directory / f'{name}-sketch.png').exists() else None
    return prepare(maps, image('line'), image('paint'), image('shade'), sketch)


def compose(layers, progress):
    """RGBA uint8 frame of the illustration at this fraction of its drawing event."""
    paint = np.where(progress >= layers['full'], 1., np.where(progress >= layers['color'], layers['cover'], 0.)) * layers['alpha']
    blend = np.clip((progress - layers['shade']) / layers['shade_ramp'], 0, 1)[..., None]
    rgb, alpha = layers['flat'] + (layers['shade_rgb'] - layers['flat']) * blend, paint.astype(np.float32)
    if 'sketch' in layers:
        start, length = layers['sketch_fade']
        visible = layers['sketch_alpha'] * np.clip((progress - layers['sketch']) * SHARP, 0, 1) * (1 - np.clip((progress - start) / length, 0, 1))
        rgb, alpha = over(layers['sketch_rgb'], visible, rgb, alpha)
    ink = layers['line_alpha'] * np.clip((progress - layers['ink']) / layers['ink_ramp'], 0, 1)
    rgb, alpha = over(layers['line_rgb'], ink, rgb, alpha)
    return np.dstack([np.rint(np.clip(rgb, 0, 1) * 255), np.rint(np.clip(alpha, 0, 1) * 255)]).astype(np.uint8)
