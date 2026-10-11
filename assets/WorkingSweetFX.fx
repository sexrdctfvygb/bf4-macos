// SPDX-License-Identifier: MIT
// ReShade adapter for the tested four-effect pipeline. Upstream functions
// are downloaded separately from the official SweetFX 1.5.1 distribution.
#include "ReShade.fxh"

#define screen_size float2(BUFFER_WIDTH, BUFFER_HEIGHT)
#define px BUFFER_RCP_WIDTH
#define py BUFFER_RCP_HEIGHT
#define s0 ReShade::BackBuffer
#define myTex2D(s, p) tex2D(s, p)
#define sharp_strength 0.85
#define sharp_clamp 0.035
#define pattern 2
#define offset_bias 1.0
#define show_sharpen 0
#define RGB_Lift float3(0.971, 1.000, 1.010)
#define RGB_Gamma float3(0.971, 1.000, 1.010)
#define RGB_Gain float3(1.005, 1.000, 1.060)
#define Curves_mode 1
#define Curves_contrast 0.53
#define Curves_formula 2
#define dither_method 1

#include "Legacy/LumaSharpen.h"
#include "Legacy/LiftGammaGain.h"
#include "Legacy/Curves.h"
#include "Legacy/Dither.h"

float4 WorkingSweetFXPS(float4 position : SV_Position, float2 uv : TEXCOORD) : SV_Target
{
    float4 color = tex2D(ReShade::BackBuffer, uv);
    color = LumaSharpenPass(color, uv);
    color = LiftGammaGainPass(color);
    color = CurvesPass(color);
    color = DitherPass(color, uv);
    return color;
}

technique WorkingSweetFX <
    ui_label = "BF4 Working SweetFX";
    ui_tooltip = "LumaSharpen, LiftGammaGain, Curves and ordered Dither.";
>
{
    pass
    {
        VertexShader = PostProcessVS;
        PixelShader = WorkingSweetFXPS;
    }
}
