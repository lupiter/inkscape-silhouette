#!/usr/bin/env python3
#
# Inkscape extension for driving a silhouette cameo
# (C) 2013 jw@suse.de. Licensed under CC-BY-SA-3.0 or GPL-2.0 at your choice.
# (C) 2014 - 2022  juewei@fabmail.org and contributors

__version__ = "1.27"     # Keep in sync with sendto_silhouette.inx ca line 179
__author__ = "Juergen Weigert <juergen@fabmail.org> and contributors"

import sys, os, time, math, operator

# we sys.path.append() the directory where this script lives.
sys.path.append(os.path.dirname(os.path.abspath(sys.argv[0])))

sys_platform = sys.platform.lower()
if sys_platform.startswith("win"):
    sys.path.append(r"C:\Program Files\Inkscape\share\inkscape\extensions")

elif sys_platform.startswith("darwin"):
    sys.path.append("/Applications/Inkscape.app/Contents/Resources/share/inkscape/extensions")

else:   # linux
    sys.path.append("/usr/share/inkscape/extensions")

# We will use the inkex module with the predefined Effect base class.
# As of Inkscape 1.1, inkex cannot be loaded if stdout is closed,
# which it might be if we are coming here via silhouette_multi.
# This manipulation of sys.stdout should be removed if the issue
# https://gitlab.com/inkscape/extensions/-/issues/412
# is resolved in a future version.
dummy_stdout=False
if not sys.stdout:
    sys.stdout=os.fdopen(os.open(os.devnull, os.O_WRONLY|os.O_APPEND), 'w')
    dummy_stdout=True

if dummy_stdout:
    sys.stdout.close()
    sys.stdout=None

import inkex
from inkex.extensions import EffectExtension
from inkex import Boolean, ShapeElement, PathElement, Rectangle, Circle, Ellipse, Line, Polyline, Polygon, Group, Use, TextElement, Image, BaseElement
from inkex.paths import CubicSuperPath
from inkex.transforms import Transform
from inkex.bezier import beziersplitatt, maxdist

from gettext import gettext
from optparse import SUPPRESS_HELP
from tempfile import NamedTemporaryFile

inkex.localization.localize()

from silhouette.Graphtec import SilhouetteCameo, CAMEO_MATS
from silhouette.Strategy import MatFree
from silhouette.convert2dashes import convert2dash
import silhouette.StrategyMinTraveling
import silhouette.read_dump
from silhouette.Geometry import dist_sq, XY_a


def px2mm(px):
    """
    Convert inkscape pixels to mm.
    The default inkscape unit, called 'px' is 96dpi
    """
    return px*25.4/96


def subdivideCubicPath(sp, flat, i=1):
    """
    Break up a bezier curve into smaller curves, each of which
    is approximately a straight line within a given tolerance
    (the "smoothness" defined by [flat]).

    This is a modified version of cspsubdiv.cspsubdiv() rewritten
    to avoid recurrence.
    """

    while True:
        while True:
            if i >= len(sp):
                return

            p0 = sp[i - 1][1]
            p1 = sp[i - 1][2]
            p2 = sp[i][0]
            p3 = sp[i][1]

            b = (p0, p1, p2, p3)

            if maxdist(b) > flat:
                break

            i += 1

        one, two = beziersplitatt(b, 0.5)
        sp[i - 1][2] = one[1]
        sp[i][0] = two[2]
        p = [one[2], one[3], two[1]]
        sp[i:1] = [p]

class teeFile:
    def __init__(self, f1, f2):
        self.f1 = f1
        self.f2 = f2
    def __del__(self, *args):
        self.close()
    def write(self, content):
        self.f1.write(content)
        self.f2.write(content)
    def close(self):
        self.f1.close()
        self.f2.close()

class SendtoSilhouette(EffectExtension):
    """
    Inkscape Extension to send to a Silhouette Cameo
    """
    def __init__(self):
        # Call the base class constructor.
        EffectExtension.__init__(self)

        self.warnings = {}
        self.pathcount = 0
        self.step_scaling_factor = 1        # see also px2mm()
        self.ptFirst = None
        self.fPrevX = None
        self.fPrevY = None
        self.fX = None
        self.fY = None
        self.paths = []
        self.docTransform = Transform()

        try:
            self.tty = open("/dev/tty", "w")
        except:
            self.tty = None
        self.log = self.tty

        self.arg_parser.add_argument("--active-tab", dest = "active_tab",
                help=SUPPRESS_HELP)
        self.arg_parser.add_argument("-d", "--dashes",
                dest = "dashes", type = Boolean, default = False,
                help="convert paths with dashed strokes to separate subpaths for perforated cuts")
        self.arg_parser.add_argument("-a", "--autocrop",
                dest = "autocrop", type = Boolean, default = False,
                help="trim away top and left margin (before adding offsets)")
        self.arg_parser.add_argument("-b", "--bbox", "--bbox-only", "--bbox_only",
                dest = "bboxonly", type = Boolean, default = False,
                help="draft the objects bounding box instead of the objects")
        self.arg_parser.add_argument("-c", "--bladediameter",
                dest = "bladediameter", type = float, default = 0.9,
                help="[0..2.3] diameter of the used blade [mm], default = 0.9")
        self.arg_parser.add_argument("-C", "--cuttingmat",
                choices=list(CAMEO_MATS.keys()), dest = "cuttingmat",
                default = "cameo_12x12", help="Use cutting mat")
        self.arg_parser.add_argument("-D", "--depth",
                dest = "depth", type = int, default = -1,
                help="[0..10], or -1 for media default")
        self.arg_parser.add_argument("--log_paths",
                dest = "dump_paths", type = Boolean, default = False,
                help="Include final cut paths in log")
        self.arg_parser.add_argument("--append_logs",
                dest = "append_logs", type = Boolean, default = False,
                help="Append to log and dump files rather than overwriting")
        self.arg_parser.add_argument("--dry_run",
                dest = "dry_run", type = Boolean, default = False,
                help="Do not send commands to device (queries allowed)")
        self.arg_parser.add_argument("-g", "--strategy",
                dest = "strategy", default = "mintravel",
                choices=("mintravel", "mintravelfull", "mintravelfwd", "matfree", "zorder"),
                help="Cutting Strategy: mintravel, mintravelfull, mintravelfwd, matfree or zorder")
        self.arg_parser.add_argument("--orient_paths",
                dest = "orient_paths", default = "natural",
                choices=("natural","desy","ascy","desx","ascx"),
                help="Pre-orient paths: natural (as in svg), or [des(cending)|asc(ending)][y|x]")
        self.arg_parser.add_argument("--fuse_paths",
                dest = "fuse_paths", type = Boolean, default = True,
                help="Merge any path with predecessor that ends at its start.")
        self.arg_parser.add_argument("-l", "--sw_clipping",
                dest = "sw_clipping", type = Boolean, default = True,
                help="Enable software clipping")
        self.arg_parser.add_argument("-m", "--media", "--media-id", "--media_id",
                dest = "media", default = "132",
                choices=("100", "101", "102", "106", "111", "112", "113",
                "120", "121", "122", "123", "124", "125", "126", "127", "128", "129", "130",
                "131", "132", "133", "134", "135", "136", "137", "138", "300"),
                help="113 = pen, 132 = printer paper, 300 = custom")
        self.arg_parser.add_argument("-o", "--overcut",
                dest = "overcut", type = float, default = 0.5,
                help="overcut on circular paths. [mm]")
        self.arg_parser.add_argument("-M", "--multipass",
                dest = "multipass", type = int, default = "1",
                help="[1..8], cut/draw each path object multiple times.")
        self.arg_parser.add_argument("-p", "--pressure",
                dest = "pressure", type = int, default = 10,
                help="[1..18], or 0 for media default")
        self.arg_parser.add_argument("-P", "--sharpencorners",
                dest = "sharpencorners", type = Boolean, default = False,
                help="Lift head at sharp corners")
        self.arg_parser.add_argument("--sharpencorners_start",
                dest = "sharpencorners_start", type = float, default = 0.1,
                help="Sharpen Corners - Start Ext. [mm]")
        self.arg_parser.add_argument("--sharpencorners_end",
                dest = "sharpencorners_end", type = float, default = 0.1,
                help="Sharpen Corners - End Ext. [mm]")
        self.arg_parser.add_argument("-r", "--reversetoggle",
                dest = "reversetoggle", type = Boolean, default = False,
                help="Cut each path the other direction. Affects every second pass when multipass.")
        self.arg_parser.add_argument("-s", "--speed",
                dest = "speed", type = int, default = 10,
                help="[1..10], or 0 for media default")
        self.arg_parser.add_argument("-S", "--smoothness", type = float,
                dest="smoothness", default=.2, help="Smoothness of curves")
        self.arg_parser.add_argument("-t", "--tool",
                choices=("autoblade", "cut", "pen", "default"), dest = "tool", default = None, help="Optimize for pen or knive")
        self.arg_parser.add_argument("-T", "--toolholder",
                choices=("1", "2"), dest = "toolholder", default = None, help="[1..2]")
        self.arg_parser.add_argument("--preview",
                dest = "preview", type = Boolean, default = True,
                help="show cut pattern graphically before sending")
        self.arg_parser.add_argument("-V", "--version",
                dest = "version", action = "version", version=__version__,
                help="print the version number and exit")
        self.arg_parser.add_argument("-w", "--wait", "--wait-done", "--wait_done",
                dest = "wait_done", type = Boolean, default = False,
                help="After sending wait til device reports ready")
        self.arg_parser.add_argument("-x", "--x-off", "--x_off",
                type = float, dest = "x_off", default = 0.0, help="X-Offset [mm]")
        self.arg_parser.add_argument("-y", "--y-off", "--y_off",
                type = float, dest = "y_off", default = 0.0, help="Y-Offset [mm]")
        self.arg_parser.add_argument("-R", "--regmark",
                dest = "regmark", type = Boolean, default = False,
                help="The document has registration marks.")
        self.arg_parser.add_argument("--regsearch",
                dest = "regsearch", type = Boolean, default = False,
                help="Search for the registration marks.")
        self.arg_parser.add_argument("-X", "--reg-x", "--regwidth",
                type = float, dest = "regwidth", default = 180.0, help="X mark distance [mm]")
        self.arg_parser.add_argument("-Y", "--reg-y", "--reglength",
                type = float, dest = "reglength", default = 230.0, help="Y mark distance [mm]")
        self.arg_parser.add_argument("--rego-x",  "--regoriginx",
                type = float, dest = "regoriginx", default = 15.0, help="X mark origin from left [mm]")
        self.arg_parser.add_argument("--rego-y", "--regoriginy",
                type = float, dest = "regoriginy", default = 20.0, help="X mark origin from top [mm]")
        self.arg_parser.add_argument("-e", "--endposition", "--end-postition",
                "--end_position", choices=("start", "below"),
                dest = "endposition", default = "below", help="Position of head after cutting: start or below")
        self.arg_parser.add_argument("--end_offset", type = float,
                dest = "end_offset", default = 0.0,
                help="Adjustment to the position after cutting")
        self.arg_parser.add_argument("--logfile",
                dest = "logfile", default = None,
                help="Name of file in which to save log messages.")
        self.arg_parser.add_argument("--cmdfile",
                dest = "cmdfile", default = None,
                help="Name of file to save transcript of cutter commands.")
        self.arg_parser.add_argument("--inc_queries",
                dest = "inc_queries", type = Boolean, default = False,
                help="Include queries in cutter command transcript")
        self.arg_parser.add_argument("--force_hardware",
                dest = "force_hardware", default = None,
                help = "Override hardware model of cutting device.")
        # Can't set up the log here because arguments have not yet been parsed;
        # defer that to the top of the effect() method, which is where all
        # of the real activity happens.


    def __del__(self, *args):
        if self.log:
            self.log.close() # will always close tty if there is one


    def report(self, message, level):
        """
        Display `message` to the appropriate output stream(s).
        Each of the following `level` values encompasses all of the later ones:
            error - display to standard error
            log   - record in logfile if there is one
            tty   - write to tty and flush if there is one
        """
        if level == 'tty':
            if self.tty:
                print(message, file=self.tty)
                self.tty.flush()
            return
        if level == 'log' or level == 'error':
            if self.log:
                print(message, file=self.log)
                # That handles the tty also, because of the tee, but
                # we have to flush the tty:
                if self.tty:
                    self.tty.flush()
            if level == 'log':
                return
        print(message, file=sys.stderr)
        if level != 'error':
            # oops accidentally used an invalid level
            print(f"  ... WARNING: message issued at invalid level {level}",
                  file=sys.stderr)

    def penUp(self):
        self.fPrevX = None              # flag that we are up
        self.fPrevY = None


    def penDown(self):
        self.paths.append([(self.fX, self.fY)])
        self.fPrevX = self.fX       # flag that we are down
        self.fPrevY = self.fY


    def plotLineAndTime(self):
        """
        Send commands out the com port as a line segment (dx, dy) and a time (ms) the segment
        should take to implement
        """
        if (self.fPrevX is None):
            return
        # assuming that penDown() was called before.
        self.paths[-1].append((self.fX, self.fY))


    def plotPath(self, path, matTransform):
        # lifted from eggbot.py, gratefully bowing to the author
        """
        Plot the path while applying the transformation defined
        by the matrix [matTransform].
        """
        # turn this path into a cubicsuperpath (list of beziers)...
        d = path.get("d")
        p = CubicSuperPath(d).transform(Transform(matTransform))
        # ...and apply the transformation to each point

        # p is now a list of lists of cubic beziers [control pt1, control pt2, endpoint]
        # where the start-point is the last point in the previous segment.
        for sp in p:

            subdivideCubicPath(sp, self.options.smoothness)
            nIndex = 0

            for csp in sp:

                if nIndex == 0:
                    self.penUp()
                    self.virtualPenIsUp = True
                elif nIndex == 1:
                    self.penDown()
                    self.virtualPenIsUp = False

                nIndex += 1

                self.fX = float(csp[1][0]) / self.step_scaling_factor
                self.fY = float(csp[1][1]) / self.step_scaling_factor

                # store home
                if self.ptFirst is None:
                    self.ptFirst = (self.fX, self.fY)

                self.plotLineAndTime()
                self.fPrevX = self.fX
                self.fPrevY = self.fY


    def recursivelyTraverseSvg(self, aNodeList,
                parent_visibility="visible",
                extra_transform=Transform()):
        """
        Recursively traverse the svg file to plot out all of the
        paths.  The function keeps track of the composite transformation
        that should be applied to each path.

        This function handles path, group, line, rect, polyline, polygon,
        circle, ellipse and use (clone) elements.  Notable elements not
        handled include text.  Unhandled elements should be converted to
        paths in Inkscape.
        """
        for node in aNodeList:
            # Ignore invisible nodes
            if isinstance(node, BaseElement):
                # try:
                #     # Inkex 1.2: `cascaded_style()` considers CSS (has bad performance!!)
                #     get = node.cascaded_style().get
                # except:
                get = lambda attr, default: node.style.get(attr, node.get(attr, default))
                if get("display", "inline") == "none":
                    continue
                if not float(get("opacity", 1.0)):
                    continue
                v = get("visibility", parent_visibility)
                if v == "inherit":
                    v = parent_visibility

            if hasattr(node, "composed_transform"):
                # calculate this object's transform
                try:  # inkscape 1.2
                    transform = node.composed_transform()
                    transform = Transform(self.docTransform) @ transform
                    transform = Transform(extra_transform) @ transform
                except:  # inkscape 1.0
                    transform = node.composed_transform()
                    transform = Transform(self.docTransform) * transform
                    transform = Transform(extra_transform) * transform

            if isinstance(node, Group):
                self.recursivelyTraverseSvg(node, parent_visibility=v)

            elif isinstance(node, Use):

                # A <use> element refers to another SVG element via an xlink:href="#blah"
                # attribute.  We will handle the element by doing an XPath search through
                # the document, looking for the element with the matching id="blah"
                # attribute.  We then recursively process that element after applying
                # any necessary (x, y) translation.
                #
                # Notes:
                #  1. We ignore the height and width attributes as they do not apply to
                #     path-like elements, and
                #  2. Even if the use element has visibility="hidden", SVG still calls
                #     for processing the referenced element.  The referenced element is
                #     hidden only if its visibility is "inherit" or "hidden".

                refid = node.get("xlink:href")
                if refid:
                    # [1:] to ignore leading "#" in reference
                    path = "//*[@id='%s']" % refid[1:]
                    refnode = node.xpath(path)
                    if refnode:
                        x = float(node.get("x", "0"))
                        y = float(node.get("y", "0"))
                        # Note: the transform has already been applied
                        try:  # inkscape 1.2
                            refnode_transform = transform @ Transform("translate(%f, %f)" % (x, y))
                            # The docTransform will get applied again inside the recursive call
                            refnode_transform @= -Transform(self.docTransform)
                        except:
                              # inkscape 1.0
                            refnode_transform = transform * Transform("translate(%f, %f)" % (x, y))
                            # The docTransform will get applied again inside the recursive call
                            refnode_transform *= -Transform(self.docTransform)
                        v = node.get("visibility", v)
                        self.recursivelyTraverseSvg(refnode, parent_visibility=v, extra_transform=refnode_transform)

            elif isinstance(node, (PathElement, Rectangle, Circle, Ellipse, Line, Polyline, Polygon)):
                if v == "hidden" or v == "collapse":
                    continue
                node = node.to_path_element()
                if self.options.dashes:
                    convert2dash(node)

                self.pathcount += 1
                self.plotPath(node, transform)

            elif isinstance(node, TextElement):
                texts = []
                plaintext = ""
                for tnode in node.iterfind(".//"):   # all subtree
                    if tnode is not None and tnode.text is not None:
                        texts.append(tnode.text)
                if len(texts):
                    if "text" not in self.warnings:
                        inkex.errormsg(gettext("Warning: unable to draw text; " +
                                "please convert it to a path first. Or consider using the " +
                                "Hershey Text extension which can be installed in the " +
                                "'Render' category of extensions."))
                        self.warnings["text"] = 1
                    plaintext = "', '".join(texts)
                    self.report(f"Text ignored: '{plaintext}'", 'error')

            elif isinstance(node, Image):
                if "image" not in self.warnings:
                    inkex.errormsg(gettext("Warning: unable to draw bitmap images; " +
                            "please convert them to line art first.  Consider using the 'Trace bitmap...' " +
                            "tool of the 'Path' menu.  Mac users please note that some X11 settings may " +
                            "cause cut-and-paste operations to paste in bitmap copies."))
                    self.warnings["image"] = 1

            elif isinstance(node, BaseElement):
                # This is another known subclass of `BaseElement`
                pass
            elif not isinstance(node.tag, str):
                # This is likely an XML processing instruction such as an XML
                # comment.  lxml uses a function reference for such node tags
                # and as such the node tag is likely not a printable string.
                # Further, converting it to a printable string likely won't
                # be very useful.
                pass
            else:
                if str(node.tag) not in self.warnings:
                    t = str(node.tag).split("}")
                    self.report(gettext(
                        f"Warning: unable to draw <{str(t[-1])}> object,"
                        f"please convert it to a path first."),
                            'error')
                    self.warnings[str(node.tag)] = 1


    def handleViewBox(self):
        """
        Set up the document-wide transform in the event that the document has an SVG viewbox
        """
        try:  # inkscape 1.2
            self.report(f"7 svg.viewport_height = {self.svg.viewport_height}", 'tty')
            self.report(f"8 svg.viewport_width = {self.svg.viewport_width}", 'tty')
            self.docTransform = Transform(scale=(self.svg.scale))
        except:  # inkscape 1.0
            self.report(f"7 svg.height = {self.svg.height}", 'tty')
            self.report(f"8 svg.width = {self.svg.width}", 'tty')
            viewbox = self.svg.get_viewbox()
            if all((viewbox[2], viewbox[3])):
                    sx = self.svg.width / viewbox[2]
                    sy = self.svg.height / viewbox[3]
                    self.docTransform = Transform(scale=(sx, sy))


    @staticmethod
    def is_closed_path(path) -> bool:
        return dist_sq(XY_a(path[0]), XY_a(path[-1])) < 0.01


    def effect(self):
        def write_progress(done, total, msg):
            if "write_start_tstamp" not in self.__dict__:
                self.write_start_tstamp = time.time()
                self.device_buffer_perc = 0.0
            perc = 100.*done/total
            if time.time() - self.write_start_tstamp < 1.0:
                self.device_buffer_perc = perc
            buf = ""
            if self.device_buffer_perc > 1.0:
                buf = " (+%d%%)" % (self.device_buffer_perc+.5)
            self.report("%d%%%s %s\r" % (perc-self.device_buffer_perc+.5,
                                         buf, msg),
                        'tty')

        if self.options.logfile:
            mode = "a" if self.options.append_logs else "w"
            self.log = open(self.options.logfile, mode)
            if self.tty:
                self.log = teeFile(self.tty, self.log)

        command_file = None
        if self.options.cmdfile:
            mode = "ab" if self.options.append_logs else "wb"
            command_file = open(self.options.cmdfile, mode)

        try:
            # log environment information
            self.report(inkex.command.inkscape('--version').rstrip(), 'log')  # Inkscape version
            self.report("Inkscape-Silhouette: %s" % (__version__), 'log')     # Plugin version
            self.report("Path: %s" % (__file__), 'log')
            self.report("Inkex: %s" % (inkex.__version__), 'log')
            self.report("Python: %s" % (sys.executable), 'log')
            self.report("Version: %s" % (sys.version), 'log')
            self.report("Platform: %s" % (sys.platform), 'log')
            self.report("Arguments: %s" % (" ".join(sys.argv)), 'log')
        except Exception:
            pass

        try:
            dev = SilhouetteCameo(log=self.log, progress_cb=write_progress,
                                  cmdfile=command_file,
                                  inc_queries=self.options.inc_queries,
                                  dry_run=self.options.dry_run,
                                  force_hardware=self.options.force_hardware)
        except Exception as e:
            self.report(e, 'error')
            return
        state = dev.status()    # hint at loading paper, if not ready.
        self.report("status=%s" % (state), 'log')
        self.report("device version: '%s'" % dev.get_version(), 'log')

        # Viewbox handling
        self.handleViewBox()
        # Build a list of the vertices for the document's graphical elements
        if self.options.ids:
            # Traverse the selected objects
            for id in self.options.ids:
                self.recursivelyTraverseSvg([self.svg.selected[id]])
        else:
            # Traverse the entire document
            self.recursivelyTraverseSvg(self.document.getroot())

        if self.options.toolholder is not None:
            self.options.toolholder = int(self.options.toolholder)
        self.pen=None
        self.autoblade=False
        if self.options.tool == "pen":
            self.pen=True
        if self.options.tool == "cut":
            self.pen=False
        if self.options.tool == "autoblade":
            self.pen=False
            self.autoblade=True

        if self.options.orient_paths != "natural":
            index = dict(x=0,y=1)[self.options.orient_paths[-1]]
            ordered = dict(des=operator.gt, asc=operator.lt)[self.options.orient_paths[0:3]]
            oldpaths = self.paths
            self.paths = []
            oldpaths.reverse() # Since popping from old and appending to new will
                               # itself reverse
            while oldpaths:
                curpath = oldpaths.pop()
                if ordered(curpath[0][index], curpath[-1][index]):
                    curpath.reverse()
                newpath = [curpath.pop()]
                while curpath:
                    if ordered(newpath[-1][index],curpath[-1][index]):
                        newpath.append(curpath.pop())
                    else:
                        if len(newpath) == 1:
                            # Have to make some progress:
                            newpath = [curpath[-1], newpath[0]]
                            # Don't leave behind an orphan
                            if len(curpath) == 1:
                                curpath = []
                        else:
                            # Have to put end of newpath back onto curpath to
                            # keep the segment between it and rest of curpath:
                            curpath.append(newpath[-1])
                        break # stop collecting an ordered segment of curpath
                if curpath: # Some of curpath is left because it was out of order
                    oldpaths.append(curpath)
                self.paths.append(newpath)

        # scale all points to unit mm
        for path in self.paths:
            for i, pt in enumerate(path):
                path[i] = (px2mm(pt[0]), px2mm(pt[1]))

        if self.options.strategy == "matfree":
            mf = MatFree("default", scale=1.0, pen=self.pen)
            mf.verbose = 0    # inkscape crashes whenever something appears in stdout.
            self.paths = mf.apply(self.paths)
        elif self.options.strategy == "mintravel":
            self.paths = silhouette.StrategyMinTraveling.sort(self.paths)
        elif self.options.strategy == "mintravelfull":
            self.paths = silhouette.StrategyMinTraveling.sort(self.paths, entrycircular=True)
        elif self.options.strategy == "mintravelfwd":
            self.paths = silhouette.StrategyMinTraveling.sort(self.paths, entrycircular=True, reversible=False)
        # in case of zorder do no reorder

        if self.paths and self.options.fuse_paths:
            rest_paths = self.paths[1:]
            self.paths = [self.paths[0]]
            for path in rest_paths:
                if path[0] == self.paths[-1][-1]:
                    self.paths[-1].extend(path[1:])
                else:
                    self.paths.append(path)

        cut = []
        pointcount = 0
        for mm_path in self.paths:
            pointcount += len(mm_path)

            multipath = []
            multipath.extend(mm_path)

            for i in range(1, self.options.multipass):
                # if reverse continue path without lifting, instead turn with rotating knife
                if (self.options.reversetoggle):
                    mm_path = list(reversed(mm_path))
                    multipath.extend(mm_path[1:])
                # if closed path (end = start) continue path without lifting
                elif self.is_closed_path(mm_path):
                    multipath.extend(mm_path[1:])
                # else start a new path
                else:
                    cut.append(mm_path)

            # on a closed path some overlapping doesn't harm, limited to a maximum of one additional round
            overcut = self.options.overcut
            if (overcut > 0) and self.is_closed_path(mm_path):
                precut = overcut
                pfrom = mm_path[-1]
                for pprev in reversed(mm_path[:-1]):
                    dx = pprev[0] - pfrom[0]
                    dy = pprev[1] - pfrom[1]
                    dist = math.sqrt(dx*dx + dy*dy)
                    if (precut > dist):  # Full segment needed
                        precut -= dist
                        multipath.insert(0, pprev)
                        pfrom = pprev
                    else:                # only partial segement needed, create new endpoint
                        pprev = (pfrom[0]+dx*(precut/dist), pfrom[1]+dy*(precut/dist))
                        multipath.insert(0, pprev)
                        break
                pfrom = mm_path[0]
                for pnext in mm_path[1:]:
                    dx = pnext[0] - pfrom[0]
                    dy = pnext[1] - pfrom[1]
                    dist = math.sqrt(dx*dx + dy*dy)
                    if (overcut > dist):  # Full segment needed
                            overcut -= dist
                            multipath.append(pnext)
                            pfrom = pnext
                    else:                 # only partial segement needed, create new endpoint
                            pnext = (pfrom[0]+dx*(overcut/dist), pfrom[1]+dy*(overcut/dist))
                            multipath.append(pnext)
                            break

            cut.append(multipath)

        if self.options.dump_paths:
            self.report(f"Logging {len(cut)} cut paths containing "
                        f"{pointcount} points:", 'log')
            self.report(f"# driver version: {__version__}", 'log')
            self.report(f"# docname: {self.svg.get('sodipodi:docname', '')}", 'log')
            self.report(cut, 'log')

        if self.options.preview:
            if silhouette.read_dump.show_plotcuts(cut, buttons=True) > 0:
                self.report("Preview aborted.", 'log')
                return

        if self.options.pressure == 0:
            self.options.pressure = None
        if self.options.speed == 0:
            self.options.speed = None
        if self.options.depth == -1:
            self.options.depth = None
        dev.setup(media=int(self.options.media, 10),
                pen=self.pen,
                toolholder=self.options.toolholder,
                cuttingmat=self.options.cuttingmat,
                sharpencorners=self.options.sharpencorners,
                sharpencorners_start=self.options.sharpencorners_start,
                sharpencorners_end=self.options.sharpencorners_end,
                autoblade=self.autoblade,
                depth=self.options.depth,
                sw_clipping=self.options.sw_clipping,
                bladediameter=self.options.bladediameter,
                pressure=self.options.pressure,
                speed=self.options.speed)

        if self.options.autocrop:
            # this takes much longer, if we have a complext drawing
            bbox = dev.plot(pathlist=cut,
                    mediawidth=px2mm(self.svg.width),
                    mediaheight=px2mm(self.svg.height),
                    margintop=0,
                    marginleft=0,
                    bboxonly=None,         # only return the bbox, do not draw it.
                    endposition="start",
                    regmark=self.options.regmark,
                    regsearch=self.options.regsearch,
                    regwidth=self.options.regwidth,
                    reglength=self.options.reglength,
                    regoriginx=self.options.regoriginx,
                    regoriginy=self.options.regoriginy)

            if len(bbox["bbox"].keys()):
                    self.report(
                        "autocrop left=%.1fmm top=%.1fmm" % (
                            bbox["bbox"]["llx"]*bbox["unit"],
                            bbox["bbox"]["ury"]*bbox["unit"]), 'log')
                    self.options.x_off -= bbox["bbox"]["llx"]*bbox["unit"]
                    self.options.y_off -= bbox["bbox"]["ury"]*bbox["unit"]

        bbox = dev.plot(pathlist=cut,
            mediawidth=px2mm(self.svg.width),
            mediaheight=px2mm(self.svg.height),
            offset=(self.options.x_off, self.options.y_off),
            bboxonly=self.options.bboxonly,
            endposition=self.options.endposition,
            end_paper_offset=self.options.end_offset,
            regmark=self.options.regmark,
            regsearch=self.options.regsearch,
            regwidth=self.options.regwidth,
            reglength=self.options.reglength,
            regoriginx=self.options.regoriginx,
            regoriginy=self.options.regoriginy)
        if len(bbox["bbox"].keys()) == 0:
            self.report("empty page?", 'error')
        else:
            write_progress(1, 1, "bbox: (%.1f, %.1f)-(%.1f, %.1f)mm, %d points" % (
                        bbox["bbox"]["llx"]*bbox["unit"],
                        bbox["bbox"]["ury"]*bbox["unit"],
                        bbox["bbox"]["urx"]*bbox["unit"],
                        bbox["bbox"]["lly"]*bbox["unit"],
                        bbox["bbox"]["count"]))
            self.report("", 'tty')
            state = dev.status()
            write_duration = time.time() - self.write_start_tstamp
            # we took write_duration seconds for actualy cutting
            # 100-device_buffer_perc percent of all data.
            # Thus we can compute the average write speed like this:
            if write_duration > 1.0:
                percent_per_sec = (100.0-self.device_buffer_perc) / write_duration
            else:
                percent_per_sec = 1000.     # unreliable data

            wait_sec = 1
            if percent_per_sec > 1:   # prevent overflow if device_buffer_perc is almost 100
                while (percent_per_sec*wait_sec < 1.6):   # max 60 dots
                    wait_sec *= 2
            dots = "."
            while self.options.wait_done and state == "moving":
                time.sleep(wait_sec)
                self.device_buffer_perc -= wait_sec * percent_per_sec
                if self.device_buffer_perc < 0.0:
                    self.device_buffer_perc = 0.0
                write_progress(1, 1, dots)
                dots += "."
                state = dev.status()
            self.device_buffer_perc = 0.0
            write_progress(1, 1, dots)
        self.report("\nstatus=%s" % (state), 'log')


if __name__ == "__main__":
    e = SendtoSilhouette()

    if (len(sys.argv) < 2):
        # write a tempfile that is removed on exit
        tmpfile=NamedTemporaryFile(suffix=".svg", prefix="inkscape-silhouette", delete=False)
        tmpfile.write(b'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" viewBox="0 0 100 100"><path d="M 0, 0" /></svg>')
        tmpfile.close()
        e.run([tmpfile.name])
        os.remove(tmpfile.name)
    else:
        start = time.time()
        e.run()
        ss = int(time.time()-start+.5)
        mm = int(ss/60)
        ss -= mm*60
        e.report(" done. %d min %d sec" % (mm, ss), 'log')

    sys.exit(0)
