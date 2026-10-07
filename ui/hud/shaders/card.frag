#version 440
// A glass card: a translucent fill lighter at the top, a 1 px inner highlight, and a hairline border in the
// accent gradient (deep at the bottom left → bright at the top right). `hover` brightens it.
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float radius;
    float borderW;
    float hover;      // 0..1
    float lit;     // 0..1
    float borderA;    // base border strength
    vec2 size;
    vec4 fillTop;
    vec4 fillBottom;
    vec4 c0;
    vec4 c1;
    vec4 c2;
    vec4 accent;
};

float sdRound(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}

vec3 grad(float t) {
    t = clamp(t, 0.0, 1.0);
    return t < 0.5 ? mix(c0.rgb, c1.rgb, t * 2.0) : mix(c1.rgb, c2.rgb, t * 2.0 - 1.0);
}

void main() {
    vec2 uv = qt_TexCoord0;
    vec2 p = (uv - 0.5) * size;
    float d = sdRound(p, size * 0.5 - 0.5, radius);
    float aa = max(fwidth(d), 0.35) * 0.75;      // one device pixel, whatever the scale
    float inside = 1.0 - smoothstep(-aa, aa, d);

    // fill
    vec4 f = mix(fillTop, fillBottom, smoothstep(0.0, 1.0, uv.y)) * (1.0 + 0.25 * hover);   // premultiplied
    vec3 rgb = f.rgb * inside;
    float a = f.a * inside;

    // inner top highlight (the glass edge)
    float top = (1.0 - smoothstep(0.0, 1.2, abs(d + 1.6))) * smoothstep(0.55, 0.0, uv.y) * 0.07;
    rgb += vec3(1.0) * top;
    a += top;

    // gradient hairline
    float t = clamp(uv.x * 0.6 + (1.0 - uv.y) * 0.4, 0.0, 1.0);
    float bw = borderW;
    float b = 1.0 - smoothstep(bw * 0.5 - aa, bw * 0.5 + aa, abs(d + bw * 0.5));
    float strength = borderA * mix(0.45, 1.0, t) + 0.35 * hover + 0.25 * lit;
    vec3 bc = mix(grad(t), accent.rgb, 0.25 * lit);
    rgb = rgb * (1.0 - b * strength) + bc * b * strength;
    a = a * (1.0 - b * strength) + b * strength;
    fragColor = vec4(rgb, clamp(a, 0.0, 1.0)) * qt_Opacity;
}
