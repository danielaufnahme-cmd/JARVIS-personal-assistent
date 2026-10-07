#version 440
// The HUD's fine grid, like JARVIS's, in its own pass so the entrance can reveal it with render-thread transforms:
// very low contrast, clearest around the core and fading toward the edges, every fourth line a touch stronger.
// `offset` drifts it a few px a minute (and leans it away from the mouse) once the HUD has settled.
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float spacing;    // px between lines
    vec2 size;        // px
    vec2 offset;      // px
    vec2 focusAt;     // px
    vec4 gridc;       // premultiplied (Qt hands colours over that way)
};

float lines(vec2 px, float s) {
    vec2 g = abs(fract(px / s + 0.5) - 0.5) * s;
    vec2 w = max(fwidth(px), vec2(0.35));
    vec2 l = 1.0 - smoothstep(vec2(0.0), w * 1.1, g);
    return max(l.x, l.y);
}

void main() {
    vec2 px = qt_TexCoord0 * size;
    vec2 gp = px + offset;
    float fine = lines(gp, spacing);
    float major = lines(gp, spacing * 4.0);
    float fade = 0.35 + 0.65 * exp(-pow(length(px - focusAt) / (size.y * 0.75), 2.0));
    float k = (fine * 0.55 + major * 0.45) * fade;
    fragColor = gridc * k * qt_Opacity;
}
