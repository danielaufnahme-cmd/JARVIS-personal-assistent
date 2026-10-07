#version 440
// The core's light: a breathing aura, a voice spectrum wrapped around the G mark (the last ~1.3 s of the real
// audio level, newest at the top, mirrored down both sides), accent-gradient comets sweeping along two rings,
// a gradient ring around the disc, a "working" spinner and a pulse wave on every state change.
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float time;
    float level;      // 0..1, eased
    float energy;     // 0..1
    float spec;       // 0..1: spectrum visibility
    float think;      // 0..1: working spinner visibility
    float pulse;      // 0..1 travelling wave (1 = done)
    float breath;     // 0..1
    float discR;      // disc radius / outer radius
    float rotA;       // degrees: the reference ring's angle
    float rotB;       // degrees: the inner scanner's angle
    float rotC;       // degrees: the disc ring's gradient
    float ringA;      // 0..1: disc ring opacity
    float tint;       // 0..1: the gradient leans into the accent (confirm, IN CONTROL, offline)
    vec4 accent;
    vec4 c0;
    vec4 c1;
    vec4 c2;
    vec4 h0; vec4 h1; vec4 h2; vec4 h3; vec4 h4; vec4 h5; vec4 h6; vec4 h7;
};

const float PI = 3.14159265;
const float TAU = 6.2831853;

vec3 grad(float t) {
    t = clamp(t, 0.0, 1.0);
    vec3 g = t < 0.5 ? mix(c0.rgb, c1.rgb, t * 2.0) : mix(c1.rgb, c2.rgb, t * 2.0 - 1.0);
    return mix(g, accent.rgb * (0.75 + 0.35 * t), tint);
}

float hist(int i) {
    int j = i / 4;
    int k = i - j * 4;
    vec4 v = j == 0 ? h0 : j == 1 ? h1 : j == 2 ? h2 : j == 3 ? h3 : j == 4 ? h4 : j == 5 ? h5 : j == 6 ? h6 : h7;
    return k == 0 ? v.x : k == 1 ? v.y : k == 2 ? v.z : v.w;
}

float histI(float x) {
    float i0 = floor(clamp(x, 0.0, 31.0));
    float i1 = min(31.0, i0 + 1.0);
    return mix(hist(int(i0)), hist(int(i1)), x - i0);
}

float px = 0.0;    // one device pixel in units of the outer radius (set from fwidth in main)

// Anti-aliased thin ring.
float ring(float rad, float R, float w) {
    return 1.0 - smoothstep(w * 0.5 - px, w * 0.5 + px, abs(rad - R));
}

void main() {
    vec2 p = (qt_TexCoord0 - 0.5) * 2.0;
    float rad = length(p);
    px = max(fwidth(rad), 0.0005);
    float ang = atan(p.x, -p.y);                 // 0 at the top, clockwise
    float u = ang / TAU + 0.5;                   // 0..1 around (0.5 = top)
    vec3 rgb = vec3(0.0);
    float alpha = 0.0;

    // ── aura: a soft halo around the disc, breathing at idle, swelling with the voice ──
    float outside = max(0.0, rad - discR);
    float I = (0.10 + 0.20 * energy) * (0.75 + 0.25 * breath) + 0.32 * level;
    float halo = exp(-outside * 9.0) * step(discR * 0.98, rad) * I;
    float haze = exp(-rad * 2.6) * (0.05 + 0.06 * energy + 0.08 * level);
    vec3 auraC = mix(accent.rgb, grad(0.55 + 0.45 * sin(ang + time * 0.3)), 0.45);
    rgb += auraC * (halo + haze);
    alpha += (halo + haze) * 0.85;

    // ── comets along the reference ring (0.86) and the inner track (0.64) ──
    float fa = fract(u - rotA / 360.0);
    float tailA = pow(fa, 5.0);
    float wA = ring(rad, 0.86, 2.2 * px) + exp(-pow((rad - 0.86) / 0.02, 2.0)) * 0.35;
    float kA = tailA * wA * (0.25 + 0.75 * energy);
    rgb += grad(fa) * kA;
    alpha += kA;

    float fb = fract(-u - rotB / 360.0);
    float tailB = pow(fb, 3.0);
    float wB = ring(rad, 0.64, 2.0 * px) + exp(-pow((rad - 0.64) / 0.015, 2.0)) * 0.3;
    float kB = tailB * wB * (0.2 + 0.8 * energy);
    rgb += grad(0.3 + 0.7 * fb) * kB;
    alpha += kB;

    // ── the disc's gradient ring ──
    float fc = fract(u - rotC / 360.0);
    float wC = ring(rad, discR, 2.4 * px);
    float gc = 0.35 + 0.65 * smoothstep(0.0, 1.0, abs(fc * 2.0 - 1.0));
    float kC = wC * ringA * gc;
    rgb += grad(fc) * kC;
    alpha += kC;

    // ── working spinner: three short comets just outside the disc ──
    float R3 = discR + 0.075;
    float f3 = fract(3.0 * (u + 0.5) - time * 0.55);
    float k3 = pow(f3, 7.0) * (ring(rad, R3, 2.6 * px) + exp(-pow((rad - R3) / 0.012, 2.0)) * 0.4) * think;
    rgb += grad(0.4 + 0.6 * f3) * k3;
    alpha += k3;

    // ── voice spectrum: 44 slim bars a side, mirrored; newest level at the top, older down the sides ──
    if (spec > 0.001) {
        float s = abs(ang) / PI;                 // 0 at the top .. 1 at the bottom
        const float N = 44.0;
        float pos = s * N;
        float id = floor(min(pos, N - 1.0));
        float f = fract(pos);
        float aaA = px / max(0.05, rad * PI / N);
        float bar = smoothstep(0.32 - aaA, 0.32 + aaA, f) * (1.0 - smoothstep(0.68 - aaA, 0.68 + aaA, f));
        float v = histI(id / (N - 1.0) * 31.0);
        v *= 0.85 + 0.15 * sin(time * 5.0 + id * 2.3);
        v *= 1.0 - 0.4 * s;
        float r0 = discR + 0.05;
        float L = 0.006 + 0.19 * clamp(v * 1.25, 0.0, 1.0);
        float inBand = smoothstep(r0 - px, r0 + px, rad) * (1.0 - smoothstep(r0 + L - px, r0 + L + px, rad));
        float tip = clamp((rad - r0) / max(L, 0.01), 0.0, 1.0);
        float k = bar * inBand * spec * mix(0.3, 0.95, tip);
        rgb += grad(0.5 + 0.5 * tip) * k;
        alpha += k * 0.9;
        // a soft bloom under the bars, smooth around the circle (no per-bar steps)
        float vs = histI(s * 31.0) * (1.0 - 0.4 * s);
        float glow = exp(-pow((rad - r0 - 0.05) / (0.04 + 0.12 * vs), 2.0)) * vs * spec * 0.16;
        rgb += grad(0.75) * glow;
        alpha += glow * 0.55;
    }

    // ── pulse wave on a state change ──
    if (pulse < 0.999) {
        float e = 1.0 - pow(1.0 - pulse, 3.0);
        float Rp = mix(discR, 0.98, e);
        float wp = mix(0.004, 0.05, e);
        float kp = exp(-pow((rad - Rp) / wp, 2.0)) * pow(1.0 - pulse, 2.0) * 0.32;
        rgb += mix(accent.rgb, c2.rgb, 0.4) * kp;
        alpha += kp;
    }

    alpha = clamp(alpha, 0.0, 1.0);
    fragColor = vec4(rgb, alpha) * qt_Opacity;
}
