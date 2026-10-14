# Converts an OpenOffice document to PDF through OpenOffice's UNO API.
# Runs under OpenOffice's bundled Python 2.7:  /opt/openoffice4/program/python docconv.py IN OUT
# A separate headless instance with its own profile does the work, so the session the agent is
# using is never touched.
import os
import subprocess
import sys
import time

import uno
from com.sun.star.beans import PropertyValue

PIPE = "chalkdocconv"
FILTERS = [
    ("com.sun.star.sheet.SpreadsheetDocument", "calc_pdf_Export"),
    ("com.sun.star.presentation.PresentationDocument", "impress_pdf_Export"),
    ("com.sun.star.drawing.DrawingDocument", "draw_pdf_Export"),
    ("com.sun.star.text.TextDocument", "writer_pdf_Export"),
]


def prop(name, value):
    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


def main():
    src, dst = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    office = subprocess.Popen(
        [
            "/opt/openoffice4/program/soffice",
            "-headless",
            "-invisible",
            "-nologo",
            "-norestore",
            "-nofirststartwizard",
            "-env:UserInstallation=file:///tmp/docconv-profile",
            "-accept=pipe,name=%s;urp;" % PIPE,
        ]
    )
    try:
        resolver = uno.getComponentContext().ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", uno.getComponentContext()
        )
        ctx = None
        for _ in range(120):
            try:
                ctx = resolver.resolve("uno:pipe,name=%s;urp;StarOffice.ComponentContext" % PIPE)
                break
            except Exception:
                time.sleep(0.25)
        if ctx is None:
            sys.exit("could not connect to OpenOffice")
        desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        doc = desktop.loadComponentFromURL(uno.systemPathToFileUrl(src), "_blank", 0, (prop("Hidden", True),))
        if doc is None:
            sys.exit("could not open %s" % src)
        filt = next((f for svc, f in FILTERS if doc.supportsService(svc)), "writer_pdf_Export")
        doc.storeToURL(uno.systemPathToFileUrl(dst), (prop("FilterName", filt),))
        doc.close(True)
        try:
            desktop.terminate()
        except Exception:
            pass
        print("converted with %s" % filt)
    finally:
        time.sleep(0.5)
        if office.poll() is None:
            office.kill()


if __name__ == "__main__":
    main()
