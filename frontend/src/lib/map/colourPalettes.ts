export type ColourPaletteId = 'turbo' | 'viridis' | 'cividis' | 'inferno';
export type RgbaColour = [number, number, number, number];

export type ColourPalette = {
  id: ColourPaletteId;
  label: string;
  accessible: boolean;
  cssGradient: string;
};

type RgbColour = readonly [number, number, number];

export const colourPalettes: readonly ColourPalette[] = [
  {
    id: 'turbo',
    label: 'Turbo',
    accessible: false,
    cssGradient: 'linear-gradient(90deg, #23171b, #2f6fdd 22%, #2ec7a1 45%, #d9e337 68%, #f36b22 84%, #900d0d)'
  },
  {
    id: 'viridis',
    label: 'Viridis · accessible',
    accessible: true,
    cssGradient: 'linear-gradient(90deg, #440154, #3b528b 25%, #21918c 50%, #5ec962 75%, #fde725)'
  },
  {
    id: 'cividis',
    label: 'Cividis · accessible',
    accessible: true,
    cssGradient: 'linear-gradient(90deg, #00204c, #29466b 24%, #596678 48%, #8e8978 70%, #c8ad63 86%, #fee838)'
  },
  {
    id: 'inferno',
    label: 'Inferno · accessible',
    accessible: true,
    cssGradient: 'linear-gradient(90deg, #000004, #57106e 25%, #bb3754 50%, #f98e09 75%, #fcffa4)'
  }
] as const;

const paletteStops: Record<Exclude<ColourPaletteId, 'turbo'>, readonly RgbColour[]> = {
  viridis: [
    [68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37]
  ],
  cividis: [
    [0, 32, 76], [41, 70, 107], [89, 102, 118], [142, 132, 110],
    [200, 173, 97], [254, 232, 56]
  ],
  inferno: [
    [0, 0, 4], [87, 16, 110], [187, 55, 84], [249, 142, 9], [252, 255, 164]
  ]
};

function clampUnit(value: number): number {
  return Math.min(1, Math.max(0, Number.isFinite(value) ? value : 0));
}

function interpolateStops(stops: readonly RgbColour[], value: number): RgbaColour {
  const position = clampUnit(value) * (stops.length - 1);
  const lowerIndex = Math.floor(position);
  const upperIndex = Math.min(stops.length - 1, lowerIndex + 1);
  const amount = position - lowerIndex;
  const lower = stops[lowerIndex];
  const upper = stops[upperIndex];
  return [
    Math.round(lower[0] + (upper[0] - lower[0]) * amount),
    Math.round(lower[1] + (upper[1] - lower[1]) * amount),
    Math.round(lower[2] + (upper[2] - lower[2]) * amount),
    50
  ];
}

// Polynomial approximation used by the original Flask map implementation.
function turbo(value: number): RgbaColour {
  const x = clampUnit(value);
  const red = 0.13572138 + x * (4.61539260 + x * (-42.66032258 + x * (132.13108234 + x * (-152.94239396 + x * 59.28637943))));
  const green = 0.09140261 + x * (2.19418839 + x * (4.84296658 + x * (-14.18503333 + x * (4.27729857 + x * 2.82956604))));
  const blue = 0.10667330 + x * (12.64194608 + x * (-60.58204836 + x * (110.36276771 + x * (-89.90310912 + x * 27.34824973))));
  const channel = (channelValue: number) => Math.round(clampUnit(channelValue) * 255);
  return [channel(red), channel(green), channel(blue), 50];
}

export function sampleColourPalette(palette: ColourPaletteId, value: number): RgbaColour {
  return palette === 'turbo'
    ? turbo(value)
    : interpolateStops(paletteStops[palette], value);
}

function paletteLut(id: ColourPaletteId): readonly RgbaColour[] {
  return Array.from({ length: 256 }, (_, index) => sampleColourPalette(id, index / 255));
}

export const colourPaletteLuts: Record<ColourPaletteId, readonly RgbaColour[]> = {
  turbo: paletteLut('turbo'),
  viridis: paletteLut('viridis'),
  cividis: paletteLut('cividis'),
  inferno: paletteLut('inferno')
};
