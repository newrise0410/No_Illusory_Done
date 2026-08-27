- [ ] G1: the test suite passes on this checkout
  CHECK: python3 -m unittest discover -s tests -q && cat tests/nid/G1.marker
  EXPECT: NID G1
  FILES: tests/test_nid_check.py, tests/nid/G1.marker
  COVERS: R1
  RED: pass-ok
  TIMEOUT: 900
