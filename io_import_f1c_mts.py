bl_info = {
    "name": "F1 Challenge 99-02 MTS Importer",
    "author": "haunetal1990",
    "version": (0, 8, 0),
    "blender": (3, 0, 0),
    "location": "File > Import > F1 Challenge 99-02 (.mts)",
    "description": "Multi-Import, Auto-Scan, Auto-Shading, Direct-Vertex-Pivot-Offset & Material Cleanup",
    "category": "Import-Export",
}

import bpy
import struct
import os
import re
import mathutils
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator, OperatorFileListElement

def read_mts_file(filepath, context):
    file_name_upper = os.path.basename(filepath).upper()
    print(f"\n--- Importing: {os.path.basename(filepath)} ---")
    
    try:
        with open(filepath, 'rb') as f:
            data = f.read()
            
        file_size = len(data)
        magic = b"CUBE_MTS"
        H2 = data.find(magic, 16)
        if H2 == -1: H2 = 0 
            
        # 1. EXTRACT REAL MATERIAL NAMES & TEXTURES
        header_raw = data[20:H2] if H2 > 20 else data[:1000]
        matches = re.findall(b'[A-Za-z0-9_\\\\.]{3,40}\\x00', header_raw)
        
        materials = []
        current_mat = None
        
        for m in matches:
            name = m.decode('ascii', errors='ignore').strip('\x00')
            if name.upper().endswith(('.BMP', '.TGA', '.DDS', '.JPG')):
                if current_mat and name not in current_mat['textures']:
                    current_mat['textures'].append(name)
            else:
                existing = next((mat for mat in materials if mat['name'] == name), None)
                if existing:
                    current_mat = existing
                else:
                    current_mat = {'name': name, 'textures': []}
                    materials.append(current_mat)

        # Cleaned material list: Remove generic ghost names
        valid_materials = []
        for mat in materials:
            if not re.search(r'_Mat_\d+', mat['name'], re.IGNORECASE):
                valid_materials.append(mat)
                
        mat_names = [m['name'] for m in valid_materials]
        print(f"> Found real materials: {mat_names}")

        summary_offset = H2 + 280
        summary_data = struct.unpack('<12I', data[summary_offset : summary_offset + 48])

        # 2. PREPARE PIVOT & DUMMY POSITION CORRECTION
        # We read the position offset directly from the summary
        pivot_x = struct.unpack('<f', struct.pack('<I', summary_data[4]))[0]
        pivot_y = struct.unpack('<f', struct.pack('<I', summary_data[5]))[0]
        pivot_z = struct.unpack('<f', struct.pack('<I', summary_data[6]))[0]

        is_pivot_object = any(keyword in file_name_upper for keyword in ["HELMET", "HELM", "WHEEL", "TYRE", "DRIVER"])

        # Initialize offset correction (default to zero)
        ox, oy, oz = 0.0, 0.0, 0.0
        if is_pivot_object and (pivot_x != 0.0 or pivot_y != 0.0 or pivot_z != 0.0):
            ox, oy, oz = pivot_x, pivot_y, pivot_z
            print(f"> Vertex pivot offset detected for {os.path.basename(filepath)}: ({ox}, {oz}, {oy})")

        # 3. MATHEMATICAL AUTO-SCANNER FOR GEOMETRY
        blocks = []
        for i in range(H2, min(H2 + 20000, file_size - 16), 4):
            v_c, v_o, i_c, f_o = struct.unpack('<4I', data[i:i+16])
            
            if 0 < v_c < 100000 and 0 < i_c < 300000 and i_c % 3 == 0:
                stride = 0
                if f_o == v_o + (v_c * 48): stride = 48
                elif f_o == v_o + (v_c * 36): stride = 36
                elif f_o == v_o + (v_c * 52): stride = 52
                    
                if stride > 0:
                    abs_v = H2 + v_o
                    abs_f = H2 + f_o
                    if not any(b['v_o'] == abs_v for b in blocks):
                        blocks.append({
                            'v_c': v_c, 'v_o': abs_v,
                            'f_c': i_c // 3, 'f_o': abs_f,
                            'stride': stride
                        })

        if not blocks:
            print("ERROR: No valid geometry blocks found.")
            return {'CANCELLED'}

        # 4. BUILD A SINGLE MESH FROM THE BLOCKS
        global_verts = []
        global_uvs = []
        global_faces = []
        material_assignments = []
        vert_accum = 0

        for b_idx, block in enumerate(blocks):
            v_c = block['v_c']
            v_o = block['v_o']
            f_c = block['f_c']
            f_o = block['f_o']
            stride = block['stride']
            
            for v in range(v_c):
                curr_v_o = v_o + (v * stride)
                if curr_v_o + stride > file_size: break
                
                floats = struct.unpack(f'<{stride//4}f', data[curr_v_o : curr_v_o + stride])
                px, py, pz = floats[0], floats[1], floats[2]
                
                # --- IMPORTANT: APPLY DIRECT VERTEX OFFSETS ---
                # We add the pivot offset directly to the vertex coordinates
                # (Blender rotated axes: Y-value of MTS goes to Z, Z-value of MTS goes to Y)
                global_verts.append((px + ox, pz + oz, py + oy))
                
                u, v_coord = 0.0, 0.0
                if stride == 36:
                    u, v_coord = floats[6], floats[7]
                elif stride >= 48:
                    u, v_coord = floats[8], floats[9]
                    if (u == 0.0 and v_coord == 0.0) or (u == 0.0 and v_coord == 1.0):
                        u, v_coord = floats[6], floats[7]
                        
                global_uvs.append((u, 1.0 - v_coord)) 

            for f in range(f_c):
                curr_f_o = f_o + (f * 6)
                if curr_f_o + 6 > file_size: break
                idx = struct.unpack('<HHH', data[curr_f_o : curr_f_o + 6])
                
                if idx[0] < v_c and idx[1] < v_c and idx[2] < v_c:
                    if idx[0] != idx[1] and idx[1] != idx[2] and idx[0] != idx[2]:
                        global_faces.append((idx[0] + vert_accum, idx[1] + vert_accum, idx[2] + vert_accum))
                        material_assignments.append(b_idx)
                        
            vert_accum += v_c

        # 5. GENERATE IN BLENDER
        mesh_name = os.path.basename(filepath).split('.')[0]
        mesh = bpy.data.meshes.new(mesh_name)
        obj = bpy.data.objects.new(mesh_name, mesh)
        
        mesh.from_pydata(global_verts, [], global_faces)
        mesh.update()

        for poly in mesh.polygons:
            poly.use_smooth = True

        uv_layer = mesh.uv_layers.new(name="UVMap")
        for loop in mesh.loops:
            uv_layer.data[loop.index].uv = global_uvs[loop.vertex_index]

        # 6. MATERIALS & REUSE
        mat_name_to_slot = {}
        
        for b_idx in range(len(blocks)):
            if b_idx < len(mat_names):
                m_name = mat_names[b_idx]
                
                if not re.search(r'_Mat_\d+', m_name, re.IGNORECASE):
                    if m_name not in mat_name_to_slot:
                        if m_name in bpy.data.materials:
                            mat = bpy.data.materials[m_name]
                        else:
                            mat = bpy.data.materials.new(name=m_name)
                            mat.use_nodes = True
                            nodes = mat.node_tree.nodes
                            links = mat.node_tree.links
                            bsdf = nodes.get("Principled BSDF")
                            
                            mat_info = next((m for m in valid_materials if m['name'] == m_name), None)
                            if mat_info and mat_info['textures']:
                                
                                def load_img(t_name):
                                    t_path = os.path.join(os.path.dirname(filepath), t_name)
                                    if not os.path.exists(t_path):
                                        base = os.path.splitext(t_path)[0]
                                        for ext in ['.tga', '.TGA', '.dds', '.DDS', '.bmp', '.BMP']:
                                            if os.path.exists(base + ext):
                                                t_path = base + ext
                                                break
                                    if os.path.exists(t_path):
                                        img_name = os.path.basename(t_path)
                                        img = bpy.data.images.get(img_name)
                                        if not img:
                                            img = bpy.data.images.load(t_path)
                                        t_node = nodes.new('ShaderNodeTexImage')
                                        t_node.image = img
                                        return t_node
                                    return None

                                tex1 = load_img(mat_info['textures'][0])
                                tex2 = load_img(mat_info['textures'][1]) if len(mat_info['textures']) > 1 else None

                                if tex1 and not tex2:
                                    tex1.location = (-300, 0)
                                    links.new(tex1.outputs['Color'], bsdf.inputs['Base Color'])
                                
                                elif tex1 and tex2:
                                    tex1.location = (-600, 100)
                                    tex2.location = (-600, -200)
                                    
                                    try:
                                        mix = nodes.new('ShaderNodeMixRGB')
                                        mix.blend_type = 'MIX'
                                        mix.inputs['Fac'].default_value = 0.5
                                        links.new(tex1.outputs['Color'], mix.inputs['Color1'])
                                        links.new(tex2.outputs['Color'], mix.inputs['Color2'])
                                        links.new(mix.outputs['Color'], bsdf.inputs['Base Color'])
                                    except:
                                        mix = nodes.new('ShaderNodeMix')
                                        mix.data_type = 'RGBA'
                                        mix.blend_type = 'MIX'
                                        mix.inputs[0].default_value = 0.5
                                        links.new(tex1.outputs['Color'], mix.inputs[6])
                                        links.new(tex2.outputs['Color'], mix.inputs[7])
                                        links.new(mix.outputs[2], bsdf.inputs['Base Color'])
                                        
                                    mix.location = (-300, 0)

                        mesh.materials.append(mat)
                        mat_name_to_slot[m_name] = len(mesh.materials) - 1
                
        for poly, b_idx in zip(mesh.polygons, material_assignments):
            if b_idx < len(mat_names):
                m_name = mat_names[b_idx]
                poly.material_index = mat_name_to_slot.get(m_name, 0)
            else:
                poly.material_index = 0

        # Link object to scene
        bpy.context.collection.objects.link(obj)
        obj.select_set(True)
        print(f"--- {mesh_name} SUCCESSFULLY IMPORTED ---")
        return {'FINISHED'}
            
    except Exception as e:
        print(f"Error with {os.path.basename(filepath)}: {str(e)}")
        import traceback
        traceback.print_exc()
        return {'CANCELLED'}

class ImportF1CMTS(Operator, ImportHelper):
    bl_idname = "import_scene.f1c_mts"
    bl_label = "Import F1C MTS"
    bl_options = {'REGISTER', 'UNDO'}
    
    filename_ext = ".mts"
    filter_glob: StringProperty(default="*.mts", options={'HIDDEN'}, maxlen=255)
    
    files: CollectionProperty(type=OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})
    directory: StringProperty(subtype='DIR_PATH')

    def execute(self, context):
        for file in self.files:
            filepath = os.path.join(self.directory, file.name)
            read_mts_file(filepath, context)
            
        try:
            bpy.ops.view3d.view_selected(use_all_regions=False)
        except: pass
            
        return {'FINISHED'}

def menu_func_import(self, context):
    self.layout.operator(ImportF1CMTS.bl_idname, text="F1 Challenge 99-02 (.mts)")

def register():
    bpy.utils.register_class(ImportF1CMTS)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)

def unregister():
    bpy.utils.unregister_class(ImportF1CMTS)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)

if __name__ == "__main__":
    register()
