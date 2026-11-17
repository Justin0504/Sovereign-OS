# README figures

Each figure is an HTML page rendered to a 2x PNG by headless Chrome, in light and dark.
The numbers in `measured.html` come from `python -m sovereign_os.bench.ruin --paper` and
`docs/CALIBRATION_FINDINGS.md`; change them there first, then here.

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
  --remote-debugging-port=9333 --user-data-dir=/tmp/fig-profile --allow-file-access-from-files about:blank &
for n in hero pipeline measured; do
  node render.mjs "$PWD/$n.html" "../$n-light.png" 1400
  node render.mjs "$PWD/$n.html" "../$n-dark.png" 1400 dark
done
```

`console-*.png` is a composite of real console screenshots with illustrative mission data.
