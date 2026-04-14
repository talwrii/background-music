# background-music
Schedule music to play in the background. Cron for music.

Detects changes to the config file immeidately.
Plays music with pygame. AI-generated and unreviewed - but I use it.

## Motivation
Plan the music you want to play ahead of time so you actually listen to it. 
Idem vs ipse vs attention.

I don't like having to mess with my phone to play music because it a distraction machine.  An alternative to algorithms.

I have this running on a raspberry pi 2 with a usb a dongle.

## Alternatives and prior work
There is a tool called `musicron` focused on playing projects. I could not find the home page.
`cmus` is a command line tool which has a playlist and a remote contro.

You could use spotify and just play music with algorithms or use your phone.

## Installation
pipx install background-music


## Usage
Write config ike so

```
9-10 file.wav
10-11 random:file2.wav,file3.wav
11-13 paylist.txt # work through the playlist
13-14 random:playlist.txt

Mon 18:30-18:40  monday.wav
Tue 18:30-18:40  tuesday.wav
Wed,Thu 18:30-18:40  mid-week.wav
```

Then run: `bgmus config`

`continuing` - keep on playing through a playlist or direction between different sessions
`continuing+random` - keep on playing through a playlist then randomize until the session ends.

## Hacking
I encouraage your to fork this and call it `bgmusic-something`. You can then tell me about this fork on github as a PR and I will loot your features :D and cite you.

## About me
I make tools for reading and agency.

I also mess around with music and music technology and home automation in my spare time.
If this sounds interesting you might like to check out r/musicaltreadmilldesk about my music desk.



