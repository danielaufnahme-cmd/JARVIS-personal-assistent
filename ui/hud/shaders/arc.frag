#version 440
// An anti-aliased arc with round caps, coloured c0 → c1 along its sweep, over an optional track; a soft glow
// sits on its head. Gauges and the clock's seconds use it.
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float thickness;  // px
    float start;      // rad, 0 = up, clockwise
    float sweep;      // rad
    float trackSweep; // rad
    float glow;       // 0..1
    vec2 size;        // px
    vec4 c0;
    vec4 c1;
    vec4 track;
};

const float TAU = 6.2831853;

// alpha of an arc of radius R from `st` sweeping `sw`, and how far along it the pixel is (0..1)
vec2 arc(vec2 p, float R, float st, float sw) {
    if (sw <= 0.0001)
        return vec2(0.0);
    float rad = length(p);
    float ang = atan(p.x, -p.y);
    float a = mod(ang - st + 2.0 * TAU, TAU);
    vec2 cs = R * vec2(sin(st), -cos(st));
    vec2 ce = R * vec2(sin(st + sw), -cos(st + sw));
    float ds = length(p - cs), de = length(p - ce);
    float d = a <= sw ? abs(rad - R) : min(ds, de);
    float t = a <= sw ? a / sw : (ds < de ? 0.0 : 1.0);
    if (sw >= TAU - 0.001) { d = abs(rad - R); t = a / TAU; }
    float h = thickness * 0.5;
    float aa = max(fwidth(rad), 0.35) * 0.85;   // one device pixel, whatever the scale
    return vec2(1.0 - smoothstep(h - aa, h + aa, d), t);
}

void main() {
    vec2 p = (qt_TexCoord0 - 0.5) * size;
    float R = min(size.x, size.y) * 0.5 - thickness * 0.5 - 4.0;
    vec2 tr = arc(p, R, start, trackSweep);
    vec2 v = arc(p, R, start, min(sweep, TAU));
    // colour uniforms arrive premultiplied
    vec4 col = mix(c0, c1, v.y) * v.x;
    vec3 rgb = track.rgb * tr.x * (1.0 - col.a) + col.rgb;
    float a = track.a * tr.x * (1.0 - col.a) + col.a;
    if (glow > 0.0 && sweep > 0.001) {
        vec2 ce = R * vec2(sin(start + sweep), -cos(start + sweep));
        float g = exp(-pow(length(p - ce) / (thickness * 2.2), 2.0)) * glow * 0.6;
        rgb += c1.rgb * g;          // c1 is opaque in practice; premultiplied either way
        a += g;
    }
    fragColor = vec4(rgb, clamp(a, 0.0, 1.0)) * qt_Opacity;
}
