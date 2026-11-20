/* Vorschau desselben Renderers ohne Linux, DRM oder HDMI. */
#include "render.h"
#include <stdio.h>
#include <stdlib.h>
int main(int argc,char **argv) {
    if (argc!=6) {
        fprintf(stderr,"Aufruf: vorschau Schriftverzeichnis Breite Höhe Zustand Ausgabe.ppm\n");
        return 1;
    }
    struct artwork a;
    if (artwork_open(&a,atoi(argv[2]),atoi(argv[3]),argv[1])) return 1;
    artwork_draw(&a,atoi(argv[4]),atoi(argv[4])==5 ? "http://192.0.2.10:8080/" : "");
    artwork_progress(&a,a.pixels,a.stride*4,atoi(argv[4]),24);
    FILE *f=fopen(argv[5],"wb");
    if (!f) { artwork_close(&a); return 1; }
    fprintf(f,"P6\n%u %u\n255\n",a.width,a.height);
    for (unsigned y=0;y<a.height;y++) for (unsigned x=0;x<a.width;x++) {
        uint32_t p=a.pixels[y*a.stride+x];
        unsigned char rgb[3]={p>>16,p>>8,p};
        if (fwrite(rgb,1,3,f)!=3) { fclose(f); artwork_close(&a); return 1; }
    }
    artwork_close(&a);
    return fclose(f)!=0;
}
