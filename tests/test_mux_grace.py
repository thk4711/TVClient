"""Compile the actual native patch's handover policy, not a Python translation."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'buildroot/external/patches/tvheadend/0001-idle-mux-grace.patch'


class NativeMuxGraceTests(unittest.TestCase):
    def test_reviewed_patch_hash(self):
        manifest = json.loads(PATCH.with_name('idle-mux-grace.json').read_text())
        self.assertEqual(hashlib.sha256(PATCH.read_bytes()).hexdigest(), manifest['patch_sha256'])
        self.assertEqual(len(manifest['files']), 4)
        if (ROOT / '.git').exists() and shutil.which('git'):
            result = subprocess.run(['git', 'check-ignore', '--no-index', str(PATCH.relative_to(ROOT)),
                                     str(PATCH.with_name('idle-mux-grace.json').relative_to(ROOT))],
                                    cwd=ROOT, capture_output=True)
            self.assertEqual(result.returncode, 1)
            private = subprocess.run(['git', 'check-ignore', '--no-index',
                                      'buildroot/external/patches/tvheadend/password-database.json'],
                                     cwd=ROOT, capture_output=True)
            self.assertEqual(private.returncode, 0)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler required')
    def test_native_deadline_reuse_force_failure_and_disable_policy(self):
        added = '\n'.join(line[1:] for line in PATCH.read_text().splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        mux_patch = PATCH.read_text().split('+++ b/src/input/mpegts/mpegts_mux.c', 1)[1].split('--- a/', 1)[0]
        mux_added = '\n'.join(line[1:] for line in mux_patch.splitlines() if line.startswith('+'))
        policy = '/* Idle grace' + mux_added.split('/* Idle grace', 1)[1]
        stub = r'''
#include <assert.h>
#include <stddef.h>
typedef int mtimer_t;
typedef struct { int mi_idle_grace, mi_enabled; long mi_last_dispatch; } mpegts_input_t;
typedef struct { mpegts_input_t *mmi_input; } mpegts_mux_instance_t;
typedef struct {
  mpegts_mux_instance_t *mm_active;
  int mm_idle_grace_pending, subscribers, enabled;
  mtimer_t mm_idle_grace_timer;
  char *mm_nicename;
} mpegts_mux_t;
#define SM_CODE_OK 0
#define LS_MPEGTS 0
#define ms2mono(x) (x)
#define sec2mono(x) (x)
static long mclk(void) { return 100; }
static long atomic_add_s64(long *p, int n) { return *p+n; }
#define tvhdebug(...) ((void)0)
static int stopped, armed, delay;
static void (*callback)(void*);
static void mtimer_disarm(mtimer_t *t) { *t=0; }
static void mtimer_arm_rel(mtimer_t *t, void (*cb)(void*), void *aux, int d) {
  (void)aux; *t=1; armed++; delay=d; callback=cb;
}
static int mpegts_mux_is_enabled(mpegts_mux_t *m) { return m->enabled; }
static int mpegts_mux_has_subscribers(mpegts_mux_t *m, char *n) { (void)n; return m->subscribers; }
static void mpegts_mux_stop_now(mpegts_mux_t *m, int force, int reason) {
  (void)reason;
  if (!force && m->subscribers) return;
  mtimer_disarm(&m->mm_idle_grace_timer); m->mm_idle_grace_pending=0;
  stopped++; m->mm_active=NULL;
}
'''
        guard = added.split('  /* A failed/unfinished service', 1)[1].split('    stop_reason = SM_CODE_ABORTED;', 1)[0]
        policy += '''
#define STYPE_STD 1
#define TSS_PACKETS 8
#define TSS_ERRORS 0xffff0000
#define TSS_NO_ACCESS 16
#define SM_CODE_ABORTED 1
typedef struct { int s_type, s_streaming_status; } mpegts_service_t;
static int service_reason(mpegts_input_t *mi, mpegts_service_t *s) {
int stop_reason=SM_CODE_OK;
  /* A failed/unfinished service''' + guard + '''    stop_reason = SM_CODE_ABORTED;
return stop_reason;
}
'''
        frontend_patch = PATCH.read_text().split('+++ b/src/input/mpegts/satip/satip_frontend.c', 1)[1]
        self.assertIn('+      .def.i    = 1000,', frontend_patch)
        self.assertIn('before loading saved config (including zero)', frontend_patch)
        default_line = next(line[1:] for line in frontend_patch.splitlines()
                            if line.startswith('+  lfe->mi_idle_grace ='))
        policy += 'static void frontend_defaults(mpegts_input_t *lfe) {' + default_line + '}\n'
        checks = r'''
int main(void) {
  mpegts_input_t fresh={0}; assert(fresh.mi_idle_grace==0); /* non-SAT-IP unchanged */
  frontend_defaults(&fresh); assert(fresh.mi_idle_grace==1000);
  fresh.mi_idle_grace=0; assert(fresh.mi_idle_grace==0); /* saved zero overrides seed */
  mpegts_input_t input={1000,1,100}; mpegts_mux_instance_t instance={&input};
  mpegts_mux_t mux={&instance,0,0,1,0,"test"};
  mpegts_mux_stop(&mux,0,0); assert(stopped==0 && armed==1 && delay==1000);
  mpegts_mux_stop(&mux,0,0); assert(armed==1); /* repeated stops cannot extend */
  mux.subscribers=1; callback(&mux); assert(stopped==0 && !mux.mm_idle_grace_pending);
  mux.subscribers=0; mpegts_mux_stop(&mux,0,0); callback(&mux);
  assert(stopped==1 && !mux.mm_active); /* no subscriber: finite expiry */
  mux.mm_active=&instance; mpegts_mux_stop(&mux,0,0); mux.subscribers=1;
  mpegts_mux_stop(&mux,0,0); assert(!mux.mm_idle_grace_timer && !mux.mm_idle_grace_pending);
  mux.subscribers=0; input.mi_idle_grace=9000; mpegts_mux_stop(&mux,0,0);
  assert(delay==2000); mpegts_mux_stop(&mux,1,0); assert(stopped==2);
  mux.mm_active=&instance; mpegts_mux_stop(&mux,0,9); assert(stopped==3); /* failure */
  mux.mm_active=&instance; input.mi_enabled=0; mpegts_mux_stop(&mux,0,0); assert(stopped==4);
  mux.mm_active=&instance; input.mi_enabled=1; mux.enabled=0;
  mpegts_mux_stop(&mux,0,0); assert(stopped==5);
  mux.mm_active=&instance; mux.enabled=1; input.mi_idle_grace=0;
  mpegts_mux_stop(&mux,0,0); assert(stopped==6); /* explicit legacy override */
  mux.mm_active=&instance; input.mi_idle_grace=-1;
  mpegts_mux_stop(&mux,0,0); assert(stopped==7);
  mux.mm_active=&instance; input.mi_idle_grace=1000; input.mi_last_dispatch=90;
  mpegts_mux_stop(&mux,0,0); assert(stopped==8); /* stale input: recovery retunes */
  mpegts_service_t service={1,8}; input.mi_idle_grace=1000;
  assert(service_reason(&input,&service)==0);
  service.s_streaming_status=0; assert(service_reason(&input,&service)==1);
  service.s_streaming_status=8 | 0x40000; assert(service_reason(&input,&service)==1);
  service.s_streaming_status=8 | 16; assert(service_reason(&input,&service)==1);
  input.mi_idle_grace=0; assert(service_reason(&input,&service)==0);
  return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'policy.c'
            source.write_text(stub + policy + checks)
            binary = Path(directory) / 'policy'
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)
