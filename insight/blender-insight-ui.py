# blender-insight-ui.py - the Insight dashboard's chrome, modelled and rendered rather than drawn.
#
#   blender -b -P blender-insight-ui.py -- <out_dir> [piece ...]      (default out_dir: public/assets)
#
# Pieces (each an RGBA PNG rendered front-on, orthographic, at the size it draws on screen):
#   ring_outer   the segmented gate ring - 24 chamfered blocks with a lit inner bevel. Rotates in CSS.
#   ring_inner   a finer counter-rotating ring of chevrons.
#   ring_core    the dark dish behind both, with a faint concentric groove pattern.
#   corner       one chamfered corner bracket; CSS mirrors it into all four.
#   rivetbar     a short horizontal rail used as a section divider.
#   memcore      OPTIONAL, not used by the current layout. The memory tiers are drawn directly on
#                ring_core's own engraved grooves instead. Kept because it is the standalone-panel
#                version if that layout ever comes back: 'blender ... -- <out> memcore'.
#
# House rules:
#   - render at the pixel size the element occupies, never larger: art rendered far bigger than its
#     widget turns strokes into fat tubes when the browser scales it down.
#   - rivets only in corner blocks; a rivet inside a stretched strip becomes an oval.
#   - emission for the lit edges, so the glow survives on a near-black page without a bloom pass.
#
# Palette:
#   plate   #0d1a24   deep hull blue-black
#   edge    #2f7f9e   cyan rim
#   lit     #6fd8f5   hot cyan emission
#   inlay   #1d6e5c   the green-teal glyph light inside the ring
#   amber   #c9a227   warning / secondary marker
import bpy, math, os, sys

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = os.path.abspath(argv[0]) if argv else os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "assets")
PIECES = argv[1:] or ["ring_outer", "ring_inner", "ring_core", "corner", "rivetbar"]
os.makedirs(OUT, exist_ok=True)

PLATE = (0.018, 0.052, 0.078, 1.0)
EDGE  = (0.184, 0.498, 0.620, 1.0)
LIT   = (0.435, 0.847, 0.961, 1.0)
INLAY = (0.086, 0.541, 0.408, 1.0)
AMBER = (0.788, 0.635, 0.153, 1.0)


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = 'CYCLES'
    sc.cycles.samples = 96
    sc.cycles.use_denoising = True
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = 'PNG'
    sc.render.image_settings.color_mode = 'RGBA'
    try:
        sc.cycles.device = 'GPU'
    except Exception:
        pass


def mat(name, base, rough=0.38, metal=1.0, emit=None, emit_strength=3.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = base
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    if emit is not None:
        b.inputs["Emission Color"].default_value = emit
        b.inputs["Emission Strength"].default_value = emit_strength
    return m


def camera_ortho(scale, res):
    cam_data = bpy.data.cameras.new("cam")
    cam_data.type = 'ORTHO'
    cam_data.ortho_scale = scale
    cam = bpy.data.objects.new("cam", cam_data)
    bpy.context.collection.objects.link(cam)
    cam.location = (0, 0, 12)
    bpy.context.scene.camera = cam
    sc = bpy.context.scene
    sc.render.resolution_x = res
    sc.render.resolution_y = res
    sc.render.resolution_percentage = 100


def lights():
    # key from the top-left, a cool fill from below-right, and a soft ambient so the plate is not black
    for loc, energy, size, color in (
        ((-6, 6, 9), 700, 6.0, (0.62, 0.84, 1.0)),
        ((6, -5, 7), 260, 7.0, (0.28, 0.55, 0.72)),
    ):
        d = bpy.data.lights.new("area", 'AREA')
        d.energy = energy
        d.size = size
        d.color = color
        o = bpy.data.objects.new("area", d)
        o.location = loc
        bpy.context.collection.objects.link(o)
        o.rotation_euler = (math.atan2(math.hypot(loc[0], loc[1]), loc[2]),
                            0, math.atan2(loc[1], loc[0]) + math.pi / 2)
    w = bpy.context.scene.world = bpy.data.worlds.new("w")
    w.use_nodes = True
    w.node_tree.nodes["Background"].inputs[0].default_value = (0.02, 0.05, 0.07, 1)
    w.node_tree.nodes["Background"].inputs[1].default_value = 0.18


def bevel(obj, width=0.012, segments=2):
    m = obj.modifiers.new("bev", 'BEVEL')
    m.width = width
    m.segments = segments
    m.limit_method = 'ANGLE'
    m.angle_limit = math.radians(40)


def render(name):
    bpy.context.scene.render.filepath = os.path.join(OUT, name + ".png")
    bpy.ops.render.render(write_still=True)
    print("[insight-ui] wrote", os.path.join(OUT, name + ".png"))


# ------------------------------------------------------------------------------------ ring pieces

def block(r_in, r_out, half_ang, z, thick, m):
    """One trapezoid block of a ring, as a solid with its own bevel."""
    verts, faces = [], []
    a0, a1 = -half_ang, half_ang
    for (r, a) in ((r_in, a0), (r_out, a0), (r_out, a1), (r_in, a1)):
        verts.append((r * math.cos(a), r * math.sin(a), z))
    for (r, a) in ((r_in, a0), (r_out, a0), (r_out, a1), (r_in, a1)):
        verts.append((r * math.cos(a), r * math.sin(a), z + thick))
    faces = [(0, 1, 2, 3), (7, 6, 5, 4), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
    me = bpy.data.meshes.new("blk")
    me.from_pydata(verts, [], faces)
    me.validate()
    ob = bpy.data.objects.new("blk", me)
    bpy.context.collection.objects.link(ob)
    ob.data.materials.append(m)
    bevel(ob, 0.010, 2)
    return ob


def piece_ring_outer():
    reset(); lights(); camera_ortho(2.2, 1024)
    plate = mat("plate", PLATE, rough=0.34)
    lit = mat("lit", EDGE, rough=0.20, emit=LIT, emit_strength=6.0)
    glyph = mat("glyph", INLAY, rough=0.45, emit=INLAY, emit_strength=5.5)
    n = 24
    step = 2 * math.pi / n
    for i in range(n):
        a = i * step
        b = block(0.74, 0.98, step * 0.44, 0.0, 0.10, plate)
        b.rotation_euler = (0, 0, a)
        # lit inner lip
        l = block(0.735, 0.760, step * 0.44, 0.02, 0.07, lit)
        l.rotation_euler = (0, 0, a)
        # the green glyph plate every block carries
        g = block(0.80, 0.93, step * 0.26, 0.101, 0.012, glyph)
        g.rotation_euler = (0, 0, a)
    render("ring_outer")


def piece_ring_inner():
    reset(); lights(); camera_ortho(2.2, 1024)
    lit = mat("lit", EDGE, rough=0.20, emit=LIT, emit_strength=7.0)
    plate = mat("plate", PLATE, rough=0.36)
    n = 9   # nine chevrons, as on the reference
    step = 2 * math.pi / n
    for i in range(n):
        a = i * step
        base = block(0.60, 0.70, step * 0.30, 0.0, 0.09, plate)
        base.rotation_euler = (0, 0, a)
        tip = block(0.555, 0.605, step * 0.16, 0.01, 0.075, lit)
        tip.rotation_euler = (0, 0, a)
    # a thin continuous hoop tying them together
    bpy.ops.mesh.primitive_torus_add(major_radius=0.705, minor_radius=0.008, major_segments=192, minor_segments=8)
    bpy.context.object.data.materials.append(lit)
    render("ring_inner")


def piece_ring_core():
    reset(); lights(); camera_ortho(2.2, 1024)
    plate = mat("plate", (0.020, 0.043, 0.059, 1.0), rough=0.55, metal=0.4)
    lit = mat("lit", EDGE, rough=0.3, emit=LIT, emit_strength=1.1)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.58, depth=0.06, vertices=192)
    dish = bpy.context.object
    dish.data.materials.append(plate)
    bevel(dish, 0.02, 3)
    for r in (0.20, 0.32, 0.44, 0.54):
        bpy.ops.mesh.primitive_torus_add(major_radius=r, minor_radius=0.0035, major_segments=192, minor_segments=6)
        bpy.context.object.location.z = 0.032
        bpy.context.object.data.materials.append(lit)
    render("ring_core")


# ------------------------------------------------------------------------------------ frame pieces

def piece_corner():
    reset(); lights(); camera_ortho(1.0, 256)
    plate = mat("plate", PLATE, rough=0.32)
    lit = mat("lit", EDGE, rough=0.22, emit=LIT, emit_strength=5.5)
    amber = mat("amber", AMBER, rough=0.35, emit=AMBER, emit_strength=1.6)
    # an L with the outer corner chamfered off, drawn in the top-left quadrant
    v = [(-0.46, 0.46, 0), (0.10, 0.46, 0), (0.10, 0.40, 0), (-0.34, 0.40, 0),
         (-0.40, 0.34, 0), (-0.40, -0.10, 0), (-0.46, -0.10, 0)]
    f = [(0, 1, 2, 3, 4, 5, 6)]
    me = bpy.data.meshes.new("corner"); me.from_pydata(v, [], f); me.validate()
    ob = bpy.data.objects.new("corner", me); bpy.context.collection.objects.link(ob)
    sol = ob.modifiers.new("sol", 'SOLIDIFY'); sol.thickness = 0.05
    ob.data.materials.append(plate); bevel(ob, 0.008, 2)
    # lit inner edge + one amber rivet in the block
    v2 = [(-0.385, 0.385, 0.03), (0.10, 0.385, 0.03), (0.10, 0.368, 0.03), (-0.368, 0.368, 0.03),
          (-0.368, -0.10, 0.03), (-0.385, -0.10, 0.03)]
    me2 = bpy.data.meshes.new("lit"); me2.from_pydata(v2, [], [(0, 1, 2, 3, 4, 5)]); me2.validate()
    ob2 = bpy.data.objects.new("lit", me2); bpy.context.collection.objects.link(ob2)
    ob2.data.materials.append(lit)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.022, depth=0.03, vertices=24, location=(-0.42, 0.42, 0.05))
    bpy.context.object.data.materials.append(amber)
    render("corner")


def piece_rivetbar():
    reset(); lights(); camera_ortho(2.0, 512)
    bpy.context.scene.render.resolution_y = 64
    plate = mat("plate", PLATE, rough=0.33)
    lit = mat("lit", EDGE, rough=0.25, emit=LIT, emit_strength=2.2)
    bpy.ops.mesh.primitive_cube_add(size=1)
    bar = bpy.context.object
    bar.scale = (0.98, 0.035, 0.03)
    bar.data.materials.append(plate)
    bevel(bar, 0.006, 2)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0.030, 0.02))
    cap = bpy.context.object
    cap.scale = (0.98, 0.006, 0.022)
    cap.data.materials.append(lit)
    render("rivetbar")



def piece_memcore():
    """Three nested memory tiers as concentric rings of pips.

    Radii are meaningful, not decorative: tier 0 is the small hot core that is ALWAYS resident
    (6 pips - IDENTITY / SOUL / USER / TRIGGERS / PROJECTS / index), tier 1 is the ring you reach for
    on demand (5 pips), tier 2 is the wide sparse archive you grep (24 pips). The CSS lights a pip
    per access, so the picture reads correctly only if the counts match the architecture.
    """
    reset(); lights(); camera_ortho(2.2, 1024)
    core_m  = mat("t0", (0.90, 0.55, 0.10, 1.0), rough=0.28, emit=(1.0, 0.72, 0.22, 1.0), emit_strength=7.0)
    mid_m   = mat("t1", EDGE, rough=0.26, emit=LIT, emit_strength=5.0)
    outer_m = mat("t2", INLAY, rough=0.40, emit=INLAY, emit_strength=4.0)
    plate   = mat("plate", PLATE, rough=0.36)

    for radius, count, m, pip in ((0.30, 6, core_m, 0.055), (0.58, 5, mid_m, 0.045), (0.90, 24, outer_m, 0.030)):
        # the track the pips sit on
        bpy.ops.mesh.primitive_torus_add(major_radius=radius, minor_radius=0.006,
                                         major_segments=192, minor_segments=8)
        bpy.context.object.data.materials.append(plate)
        for i in range(count):
            a = (2 * math.pi / count) * i
            bpy.ops.mesh.primitive_cylinder_add(radius=pip, depth=0.05, vertices=20,
                                                location=(radius * math.cos(a), radius * math.sin(a), 0.02))
            ob = bpy.context.object
            ob.data.materials.append(m)
            bevel(ob, 0.006, 2)

    # spokes from the core outward - the retrieval paths
    for i in range(6):
        a = (2 * math.pi / 6) * i + math.pi / 6
        bpy.ops.mesh.primitive_cube_add(size=1)
        sp = bpy.context.object
        sp.scale = (0.30, 0.004, 0.006)
        sp.location = (0.60 * math.cos(a), 0.60 * math.sin(a), 0.0)
        sp.rotation_euler = (0, 0, a)
        sp.data.materials.append(plate)
    render("memcore")


BUILD = {
    "ring_outer": piece_ring_outer, "ring_inner": piece_ring_inner, "ring_core": piece_ring_core,
    "corner": piece_corner, "rivetbar": piece_rivetbar, "memcore": piece_memcore,
}

for p in PIECES:
    fn = BUILD.get(p)
    if not fn:
        print("[insight-ui] unknown piece:", p, "- known:", ", ".join(BUILD))
        continue
    print("[insight-ui] building", p)
    fn()
print("[insight-ui] done ->", OUT)
