# SlamVision IPTV (beta)

Free M3U + XMLTV feeds so you can watch [SlamVision](https://ring-channel-tv.emergent.host/) on a Roku IPTV player. The live playlist and guide are rebuilt about every 30 minutes from the public SlamVision API. The website is not modified.

This is a beta. It will not feel like the Android TV / Fire TV app.

## Playlist links

Live channels + guide:

```
https://jhatren927-ai.github.io/slamvision-iptv/slamvision.m3u
```

TV guide (also built into the playlist header):

```
https://jhatren927-ai.github.io/slamvision-iptv/guide.xml
```

On Demand library (add as a second playlist):

```
https://jhatren927-ai.github.io/slamvision-iptv/slamvision-ondemand.m3u
```

If GitHub Pages is down, the same files are on the `gh-pages` branch:

```
https://raw.githubusercontent.com/jhatren927-ai/slamvision-iptv/gh-pages/slamvision.m3u
https://raw.githubusercontent.com/jhatren927-ai/slamvision-iptv/gh-pages/guide.xml
https://raw.githubusercontent.com/jhatren927-ai/slamvision-iptv/gh-pages/slamvision-ondemand.m3u
```

## Watch on Roku with IPTV Smart Player Lite

1. On the Roku, open the Streaming Store (or Channel Store) and search for **IPTV Smart Player Lite**.
2. Add the channel, then open it.
3. Add a playlist / M3U URL and paste the live playlist link above. Use the Roku mobile app keyboard so you can paste instead of typing.
4. Wait for the channels to load.
5. If the guide is empty, open the app’s EPG / TV guide settings, paste the guide URL, save, and refresh.
6. To browse tapes, add the On Demand link as a second playlist.

The app listing says there are no hidden fees. Availability can change, so check the store description before you rely on it.

## What works, and what does not

- **Live channels** are listed with names, numbers, categories, and a guide for the next 12 hours. The site API will not return 24 hours of guide data.
- **Tuning a live channel starts the tape that is on now, from the beginning.** It does not join mid-show. When that tape ends, playback stops until you pick the channel again. A server that re-streams 24/7 would be needed for true live TV, and that is not free.
- The playlist is refreshed every 30 minutes so the current tape stays up to date. If you stay in a channel, the player keeps the file you already started.
- **On Demand** plays each tape from the start. The list is grouped by channel and is filled in gradually so we do not overload the SlamVision site. It will grow toward the full library.
- The player will look like a generic IPTV app, not SlamVision.
- The full app, including mid-show live tuning, is on Android TV and Fire TV. In Downloader, delete any old SlamVision .apk under Files, then enter code **3059600**.

## Build it yourself

```
python3 generate.py --out output --vod always
python3 validate.py --dir output
```

`--vod auto` (used by the scheduled GitHub Action) rebuilds the On Demand list only when it is missing or older than a day, and it stops if the site API is unhealthy.

## Automatic refresh

A GitHub Actions workflow lives at `docs/refresh.workflow.yml`. It could not be installed automatically because this account’s GitHub token does not include the `workflow` scope.

To turn on the 30-minute refresh, copy that file to `.github/workflows/refresh.yml` and push, or paste it in the GitHub Actions UI. Then enable write permissions for Actions under the repo’s Settings → Actions → General.
