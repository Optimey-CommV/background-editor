<!--
SPDX-FileCopyrightText: 2026 Optimey CommV

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Why this model chain

Background Editor is built for portraits, where the hard part is not finding the person but
keeping everything that belongs to the person (hair, loose strands, beanies, hats,
headscarves, glasses, braids) while removing real background that can look similar
(dark foliage behind dark hair, black graffiti next to a black braid, a pale sky behind
blond hair). The chain below was chosen by measuring, not by reputation.

## Default chain ("Best")

1. **BiRefNet-matting** (1024 × 1024, MIT) predicts the person as a soft matte. Of the
   segmenters tested it was the only one that kept every hat, beanie, headscarf, beard,
   pair of glasses and braid on all test photos *and* gave soft alpha in hair.
2. **ViTMatte-S (Distinctions-646)** rebuilds the alpha inside a narrow edge band, in
   overlapping 1024 × 1024 tiles at a 2048 px working size, so single strands survive on
   large photos. Its RGB input is ImageNet-normalised; feeding raw 0-1 values (as rembg does)
   was about 2x worse in the edge band.
3. **Blur-fusion foreground estimation** (Forte & Pitié, ICIP 2021) removes the old
   background colour mixed into semi-transparent hair, so strands do not show a halo on a
   new background.

"Balanced" skips step 2; "Fast" uses BiRefNet-lite, the only model that ran correctly on a
4 GB GPU.

## How it was evaluated

- Ten deliberately hard, freely licensed portraits from Wikimedia Commons (nine CC0, one
  CC BY-SA 2.0): a beanie on a night street, an afro puff against a busy city, backlit
  flyaway hair, blond hair against a pale sky, a brimmed hat in a dark forest, glasses and
  a beard in front of trees, a black hijab on a dark background, a festival crowd, braids in
  front of graffiti, and big curls at dusk. The photos are not part of this repository.
- Candidates: BiRefNet portrait, general, matting and lite; BEN2 Base; and refinement
  variants (single-pass ViTMatte, tiled ViTMatte with 1 %, 3 % and asymmetric bands, with
  and without blur-fusion, a main-subject filter).
- Every result was composited on white, green and dark grey and inspected at 100 %. An
  independent reviewer scored the variants from the pixels, without trusting the
  benchmark's own notes.

## Findings that shaped the defaults

| Finding | Consequence |
|---|---|
| BiRefNet-portrait deleted a whole out-of-focus blond hair bundle (about 15 000 px) | matting model is the default |
| A symmetric 3 % refinement band deleted an entire braid | 1 % band; the band never reaches deep into the person |
| A wider outer band finds long flyaways but turns bokeh into "ghost strands" | optional extra pass, only kept where connected to the person |
| "Keep only the main person" removed a knee separated by an arm | off by default |
| BEN2 leaked a tree trunk and dropped most loose strands | not offered |
| rembg's pre-processing (divide by image max, min-max stretch) | measurable but tiny on these photos; the app uses x/255 + ImageNet and plain sigmoid |

Known residual weakness: a light haze can remain in blurred gaps between limbs (for example
between an arm and a knee).

## Speed (Intel i7-8850H, 6 cores, CPU)

| Preset | Time per photo |
|---|---|
| Best | about 60 s |
| Best + extra strand pass | about 90 s |
| Balanced | about 30 s |
| Fast | about 13 s (6 s on a GPU) |

## GPU findings (NVIDIA Quadro P1000, 4 GB, DirectML)

- The large BiRefNet models either ran out of memory or returned an all-zero mask without
  any error, then hung the device. They always run on the CPU; the app also checks every
  GPU result for this failure mode.
- ViTMatte cannot be created on DirectML (E_INVALIDARG); it always runs on the CPU.
- BiRefNet-lite runs correctly with DirectML graph fusion disabled.
- The integrated Intel GPU needed two minutes to create a session and was slower than the
  CPU; automatic mode only uses dedicated GPUs.

## Licences of the models

The BiRefNet and ViTMatte weights are published under the MIT licence, but their authors
note that some training datasets carry research-only terms. The BiRefNet author announced
(GitHub issue #306, 2026-06-06) that v2 will separate academic and commercial training and
state a licence per weights file, and that v2 is based on DINOv3, whose weights have their
own licence. The app's model updater only switches automatically when a new model passes
every technical check and its licence is an open one without such restrictions; otherwise
it only notifies.
