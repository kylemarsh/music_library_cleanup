#!/usr/bin/env python3
from urllib.parse import quote
from pathlib import Path
import sys
import os

MAC_PREFIX = "/path/to/my/music/"
CIFS_PREFIX = "x-file-cifs://nas/share/music/"

def rewrite_path(line):
    if line.startswith(MAC_PREFIX):
        rest = line[len(MAC_PREFIX):]
        encoded = "/".join(quote(part, safe="") for part in rest.split("/"))
        return CIFS_PREFIX + encoded
    return line

if __name__ == "__main__":
     input_dir = sys.argv[1]
     output_dir = sys.argv[2]

     if not Path(output_dir).is_dir():
         print(f"{output_dir} is not a directory")
         exit()

     d = Path(input_dir)
     for filename in d.iterdir():
         if filename.is_file():
            with open(filename, 'r', encoding="mac_roman") as f:
                converted = [rewrite_path(x.rstrip()) for x in f.readlines()]
                outfilename = os.path.join(output_dir, os.path.split(filename)[-1])
                with open(outfilename, 'w') as f:
                    f.write('\n'.join(converted) + '\n')
