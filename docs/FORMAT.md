# Format

`index000.bin` contains the information that stitches together the videos itself.

The file header itself is about 1280 bytes, although the file size is 268.4 MB. The file has encoded various data such as a counter for how many times files have been modified, version of the index file, which can either be shown as 2 or 3, and yes there are two known versions. A counter for av files, implying how many video files are stored inside the SD card, regardless of whether those containers actually contain video or not (remember, those files are pre-allocated). And fields for the filename in which the next recordings will be stored, the last recording that has been stored, and current file, as files are stored in the `hivxxxxx.mp4` format (so if the current file is `hiv00001.mp4`, the previous is `hiv00000.mp4` and the next is `hiv00002.mp4`). There is also an unknown field, whose meaning I cannot ascertain, probably is for padding since it only contains `\x00`s and a checksum for the file itself. The current file record on the other hand contains 12 `\x00` at the start, then a `\r` and some gibberish and continues on with `\x00` repeating a `\xff` every 22 `\00`s.

Each of these data points have an offset each, and are stored in 64-bit and 32-bit unsigned little endian 'arrays' that you can read from.

`index01.bin` is just a copy of `index00.bin`, probably for redundancy.

`index00.bin` also contains the data about the segments of the `hivxxxxx.mp4` videos. Those videos aren't actually stored in the normal format of an `.mp4` container. All of those `.mp4` files are 268.4 MB. Same as the `.bin` files. In fact, all files in the SD Card are the same size, since they are moving pre-allocated buffers, and not a normal video or photo file(s). The video files have an MPEG-PS stream containing HEVC as the video codec, and a .mp2 file as the audio codec. The videos are stored as segments inside that .mp4 files:


.mp4
________________________________________________________
|_______________________  _______________________       |
||                     |  |                     |       |    
||MPEG-PS (HEVC + mp2) |  |MPEG-PS (HEVC + mp2) | ......|
||                     |  |                     |       |
|-----------------------  -----------------------       |
|_______________________________________________________|

It is possible that those segments inside may be overwritten due to the storage being full, and as a result may be corrupted, or, the device may have been actively I/Oing when you turned off and removed the SD card. Therefore, there must be a check to see if the data is truly valid or not.

There are also another files called `index00p.bin` and `hiv00000.pic` which contain the pictures that are sent to your phone, if you use the EZVIZ app (or Hikvision), regarding human or motion detection. However, reversing that would be for another day.

The checksum in the file isn't standard CRC32. 
