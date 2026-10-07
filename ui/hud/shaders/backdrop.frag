#version 440
// The HUD ground: accent-coloured light drifting very slowly and a vignette (the grid is its own pass, grid.frag).
// `reveal`/`edge` are an optional circular reveal; the HUD keeps them off (its entrance is render-thread motion).
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float time;       // s
    float reveal;     // px: radius of the reveal circle around `origin`
    float feather;    // px: softness of its edge
    float edge;       // 0..1: the glowing front of the reveal
    float meshAmt;    // 0..1
    vec2 size;        // px
    vec2 origin;      // px
    vec4 base;
    vec4 c0;
    vec4 c1;
    vec4 c2;
    vec4 shade;
};

// Qt hands colour uniforms over premultiplied; this gives the straight colour back.
vec3 un(vec4 c) {
    return c.a > 0.0001 ? c.rgb / c.a : vec3(0.0);
}

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
}

void main() {
    vec2 px = qt_TexCoord0 * size;
    float ar = size.x / size.y;
    vec2 uv = vec2(qt_TexCoord0.x * ar, qt_TexCoord0.y);
    float t = time;
    vec3 col = un(base);

    // Three soft lights in the accent colours, drifting on slow Lissajous paths (one loop takes minutes).
    vec2 b0 = vec2(ar * (0.16 + 0.07 * sin(t * 0.043)), 0.22 + 0.10 * cos(t * 0.037));
    vec2 b1 = vec2(ar * (0.86 + 0.06 * cos(t * 0.031 + 1.7)), 0.80 + 0.09 * sin(t * 0.040));
    vec2 b2 = vec2(ar * (0.50 + 0.12 * sin(t * 0.023 + 0.6)), 0.42 + 0.12 * cos(t * 0.029 + 2.1));
    float g0 = exp(-dot(uv - b0, uv - b0) / 0.22);
    float g1 = exp(-dot(uv - b1, uv - b1) / 0.20);
    float g2 = exp(-dot(uv - b2, uv - b2) / 0.09);
    col = mix(col, un(c0), g0 * c0.a * meshAmt);
    col = mix(col, un(c1), g1 * c1.a * meshAmt);
    col = mix(col, un(c2), g2 * c2.a * meshAmt);

    // Vignette toward the shade at the edges.
    vec2 q = (qt_TexCoord0 - 0.5) * vec2(ar, 1.0);
    float v = smoothstep(0.35, 1.25, length(q) * 1.15);
    col = mix(col, un(shade), v * shade.a);

    // Dither so the gradients don't band on 8-bit.
    col += (hash(px + fract(t)) - 0.5) / 255.0;

    // Reveal: a circle growing out of the pill, with a thin glowing front.
    float dist = length(px - origin);
    float a = 1.0 - smoothstep(reveal - feather, reveal, dist);
    float front = exp(-pow((dist - reveal + feather * 0.5) / max(1.0, feather * 0.35), 2.0)) * edge;
    vec3 frontc = mix(un(c1), un(c2), 0.6) * front;
    float ga = a * base.a;                       // base.a < 1: the blurred wallpaper shows through (noctalia theme)
    fragColor = vec4(col * ga + frontc * 0.6, clamp(ga + front * 0.6, 0.0, 1.0)) * qt_Opacity;
}
