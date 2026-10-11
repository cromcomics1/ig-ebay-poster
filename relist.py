"""
Refresh stale eBay listings with "Sell similar": create a brand-new listing
copied from the old one (photos, description, price, shipping, returns) with an
improved title, extra item specifics and a condition note, then end the old one.

Two steps, both run from GitHub Actions (see relist.yml):

  MODE=propose  Pick listings (by item numbers, or by price/age rule from the
                audit) and write proposal.csv: old title, suggested title and
                suggested item specifics. Nothing on eBay changes.

  MODE=apply    For approved rows only. For each listing:
                  1. eBay verifies the new listing (no changes yet)
                  2. the old listing is ended
                  3. the new listing is created
                If step 3 fails, the old listing is relisted unchanged.
                DRY_RUN=true (the default) stops after step 1.

Files live on the 'relist-data' branch so they never collide with the poster.
Safety: max 50 per run, 1,500 per month, stops at the first failure or fee.
"""

import csv
import io
import math
import os
import re
import subprocess
import sys
import time
import urllib.error