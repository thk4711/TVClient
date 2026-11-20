#ifndef SATIP_SPLASH_RENDER_H
#define SATIP_SPLASH_RENDER_H
#include <stdint.h>
#include <ft2build.h>
#include FT_FREETYPE_H
#define ARTWORK_STATE_COUNT 39

struct artwork {
    unsigned width, height, stride;
    int german;
    int progress; /* -1: milestone for the state; otherwise animated percentage */
    uint32_t *pixels;
    FT_Library library;
    FT_Face regular, bold;
};
int artwork_open(struct artwork *a, unsigned width, unsigned height, const char *fonts);
void artwork_close(struct artwork *a);
void artwork_draw(struct artwork *a, int state, const char *detail);
void artwork_progress(struct artwork *a, void *pixels, unsigned stride, int state, unsigned tick);
int artwork_waiting(int state);
int artwork_milestone(int state);
unsigned artwork_bar_top(unsigned width, unsigned height);
unsigned artwork_bar_bottom(unsigned width, unsigned height);
#endif
