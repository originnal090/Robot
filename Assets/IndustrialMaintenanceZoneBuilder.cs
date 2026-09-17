using UnityEngine;

#if UNITY_EDITOR
using UnityEditor;
#endif

[ExecuteAlways]
[DisallowMultipleComponent]
public sealed class IndustrialMaintenanceZoneBuilder : MonoBehaviour
{
    [SerializeField] private bool rebuildInEditor = true;
    [SerializeField] private Vector3 originLocalOffset = Vector3.zero;

    private const string GeneratedRootName = "Generated_Industrial_Maintenance_Duty_Zone";

#if UNITY_EDITOR
    private bool queued;

    private void OnEnable()
    {
        QueueRebuild();
    }

    private void OnValidate()
    {
        QueueRebuild();
    }

    private void QueueRebuild()
    {
        if (!rebuildInEditor || Application.isPlaying || queued)
        {
            return;
        }

        queued = true;
        EditorApplication.delayCall += () =>
        {
            queued = false;
            if (this != null && rebuildInEditor && !Application.isPlaying)
            {
                Rebuild();
            }
        };
    }
#else
    private void Awake()
    {
        Rebuild();
    }
#endif

    [ContextMenu("Rebuild Industrial Maintenance Zone")]
    private void Rebuild()
    {
        ClearGeneratedRoot();

        var root = new GameObject(GeneratedRootName).transform;
        root.SetParent(transform, false);
        root.localPosition = originLocalOffset;
        root.localRotation = Quaternion.identity;
        root.localScale = Vector3.one;

        var yellow = CreateMaterial("Zone Yellow", new Color(1f, 0.83f, 0.05f));
        var black = CreateMaterial("Zone Black", new Color(0.02f, 0.02f, 0.02f));
        var safetyGreen = CreateMaterial("Safety Green", new Color(0.12f, 0.34f, 0.18f));
        var metal = CreateMaterial("Industrial Metal", new Color(0.46f, 0.48f, 0.47f));
        var worktop = CreateMaterial("Worktop Brown", new Color(0.45f, 0.25f, 0.12f));
        var screen = CreateMaterial("Monitor Screen", new Color(0.02f, 0.08f, 0.12f));
        var paper = CreateMaterial("Label Paper", new Color(0.88f, 0.82f, 0.65f));
        var red = CreateMaterial("Emergency Red", new Color(0.75f, 0.05f, 0.03f));

        BuildFloorZoning(root, yellow, black);
        BuildMonitoringDeskArea(root, screen, paper);
        BuildMaintenanceWorkbench(root, worktop, metal, yellow);
        BuildStorageAndSafetyWall(root, metal, safetyGreen, yellow, black, red);
    }

    private void BuildFloorZoning(Transform root, Material yellow, Material black)
    {
        AddBox(root, "Clearway_Left_Yellow_Line", new Vector3(-18.2f, 0.035f, 23.0f), new Vector3(0.10f, 0.035f, 9.4f), yellow);
        AddBox(root, "Clearway_Right_Yellow_Line", new Vector3(-12.5f, 0.035f, 23.0f), new Vector3(0.10f, 0.035f, 9.4f), yellow);
        AddBox(root, "Clearway_Front_Yellow_Line", new Vector3(-15.35f, 0.035f, 18.35f), new Vector3(5.8f, 0.035f, 0.10f), yellow);
        AddBox(root, "Clearway_Back_Yellow_Line", new Vector3(-15.35f, 0.035f, 27.65f), new Vector3(5.8f, 0.035f, 0.10f), yellow);

        for (var i = 0; i < 8; i++)
        {
            AddBox(root, $"Black_Hazard_Diagonal_{i + 1:00}", new Vector3(-18.18f + i * 0.82f, 0.05f, 27.68f), new Vector3(0.58f, 0.025f, 0.08f), black, Quaternion.Euler(0f, 35f, 0f));
        }

        AddBox(root, "Clearway_Center_Marker", new Vector3(-15.35f, 0.055f, 23.0f), new Vector3(1.1f, 0.025f, 0.16f), yellow);
        AddBox(root, "Maintenance_Zone_Marker", new Vector3(-11.2f, 0.055f, 25.8f), new Vector3(0.9f, 0.025f, 0.16f), yellow, Quaternion.Euler(0f, 90f, 0f));
        AddBox(root, "Safety_Zone_Marker", new Vector3(-8.75f, 0.055f, 24.2f), new Vector3(0.75f, 0.025f, 0.16f), yellow, Quaternion.Euler(0f, 90f, 0f));
    }

    private void BuildMonitoringDeskArea(Transform root, Material screen, Material paper)
    {
        AddPrefabOrFallback(root, "Monitoring_Chair_Tucked_In", "Assets/MarioParadiso/Built-In/Prefabs/Chair.prefab",
            new Vector3(-16.35f, 0f, 17.65f), Quaternion.Euler(0f, 0f, 0f), Vector3.one * 0.8f,
            new Vector3(0.45f, 0.75f, 0.45f), null);

        AddPrefabOrFallback(root, "Monitoring_Desktop", "Assets/MarioParadiso/Built-In/Prefabs/Desktop.prefab",
            new Vector3(-16.25f, 1.02f, 18.65f), Quaternion.Euler(0f, 180f, 0f), Vector3.one * 0.65f,
            new Vector3(0.75f, 0.12f, 0.45f), screen);

        AddBox(root, "Monitoring_Screen_Left", new Vector3(-16.75f, 1.34f, 18.9f), new Vector3(0.55f, 0.32f, 0.04f), screen);
        AddBox(root, "Monitoring_Screen_Right", new Vector3(-15.95f, 1.34f, 18.9f), new Vector3(0.55f, 0.32f, 0.04f), screen);
        AddBox(root, "Monitoring_Keyboard", new Vector3(-16.35f, 1.06f, 18.35f), new Vector3(0.7f, 0.04f, 0.18f), screen);

        AddPrefabOrFallback(root, "Duty_Radio", "Assets/MarioParadiso/Built-In/Prefabs/Radio.prefab",
            new Vector3(-15.35f, 1.08f, 18.45f), Quaternion.Euler(0f, 25f, 0f), Vector3.one * 0.45f,
            new Vector3(0.18f, 0.12f, 0.12f), screen);

        AddBox(root, "Inspection_Log_Folder", new Vector3(-17.05f, 1.08f, 18.35f), new Vector3(0.38f, 0.025f, 0.28f), paper, Quaternion.Euler(0f, -12f, 0f));
    }

    private void BuildMaintenanceWorkbench(Transform root, Material worktop, Material metal, Material yellow)
    {
        AddPrefabOrFallback(root, "Maintenance_Workbench", "Assets/MarioParadiso/Built-In/Prefabs/WorkBench.prefab",
            new Vector3(-11.15f, 0f, 26.35f), Quaternion.Euler(0f, 180f, 0f), Vector3.one * 0.95f,
            new Vector3(2.6f, 0.9f, 0.75f), worktop);

        AddPrefabOrFallback(root, "Maintenance_WorkTable", "Assets/MarioParadiso/Built-In/Prefabs/WorkTable.prefab",
            new Vector3(-11.15f, 0f, 24.75f), Quaternion.Euler(0f, 180f, 0f), Vector3.one * 0.75f,
            new Vector3(2.1f, 0.85f, 0.7f), worktop);

        AddPrefabOrFallback(root, "Workbench_Vice", "Assets/MarioParadiso/Built-In/Prefabs/Vice.prefab",
            new Vector3(-12.1f, 1.0f, 26.05f), Quaternion.Euler(0f, 45f, 0f), Vector3.one * 0.45f,
            new Vector3(0.3f, 0.16f, 0.24f), metal);

        AddPrefabOrFallback(root, "Workbench_Wrench", "Assets/MarioParadiso/Built-In/Prefabs/Wrench.prefab",
            new Vector3(-10.8f, 1.03f, 26.05f), Quaternion.Euler(0f, -25f, 90f), Vector3.one * 0.45f,
            new Vector3(0.45f, 0.04f, 0.08f), metal);

        AddPrefabOrFallback(root, "Workbench_Hammer", "Assets/MarioParadiso/Built-In/Prefabs/Hammer.prefab",
            new Vector3(-10.35f, 1.03f, 25.55f), Quaternion.Euler(0f, 25f, 90f), Vector3.one * 0.4f,
            new Vector3(0.42f, 0.05f, 0.12f), metal);

        AddPrefabOrFallback(root, "Small_Parts_Box", "Assets/MarioParadiso/Built-In/Prefabs/ScrewBox.prefab",
            new Vector3(-11.65f, 1.02f, 25.45f), Quaternion.Euler(0f, -10f, 0f), Vector3.one * 0.55f,
            new Vector3(0.45f, 0.16f, 0.28f), yellow);

        AddPrefabOrFallback(root, "Workbench_Lamp", "Assets/MarioParadiso/Built-In/Prefabs/DeskLamp.prefab",
            new Vector3(-12.55f, 1.0f, 25.55f), Quaternion.Euler(0f, 130f, 0f), Vector3.one * 0.55f,
            new Vector3(0.18f, 0.55f, 0.18f), metal);
    }

    private void BuildStorageAndSafetyWall(Transform root, Material metal, Material safetyGreen, Material yellow, Material black, Material red)
    {
        AddPrefabOrFallback(root, "Parts_Rack_A", "Assets/Material/CyberneticWalrus/Warehouse_Props_Vol1/Prefabs/Rack_01a_Prefab_01.prefab",
            new Vector3(-8.25f, 0f, 25.65f), Quaternion.Euler(0f, 90f, 0f), Vector3.one * 0.85f,
            new Vector3(1.1f, 2.0f, 2.0f), metal);

        AddPrefabOrFallback(root, "Parts_Rack_B", "Assets/Material/CyberneticWalrus/Warehouse_Props_Vol1/Prefabs/Rack_01a_Prefab_01.prefab",
            new Vector3(-8.25f, 0f, 22.65f), Quaternion.Euler(0f, 90f, 0f), Vector3.one * 0.85f,
            new Vector3(1.1f, 2.0f, 2.0f), metal);

        AddShelfLoad(root, new Vector3(-8.25f, 0f, 25.65f), yellow, metal);
        AddShelfLoad(root, new Vector3(-8.25f, 0f, 22.65f), yellow, metal);

        AddPrefabOrFallback(root, "Electrical_Safety_Cabinet", "Assets/TirgamesAssets/Factory/Prefabs/PowerBox03.prefab",
            new Vector3(-8.65f, 0f, 19.65f), Quaternion.Euler(0f, 90f, 0f), Vector3.one * 1.05f,
            new Vector3(0.75f, 1.8f, 0.42f), safetyGreen);

        AddBox(root, "Electrical_Warning_Panel", new Vector3(-8.25f, 1.55f, 19.65f), new Vector3(0.04f, 0.55f, 0.75f), yellow);
        AddBox(root, "Electrical_Warning_Bar_01", new Vector3(-8.22f, 1.58f, 19.45f), new Vector3(0.05f, 0.08f, 0.42f), black, Quaternion.Euler(0f, 0f, 28f));
        AddBox(root, "Electrical_Warning_Bar_02", new Vector3(-8.22f, 1.42f, 19.77f), new Vector3(0.05f, 0.08f, 0.42f), black, Quaternion.Euler(0f, 0f, 28f));

        AddPrefabOrFallback(root, "Wall_Fire_Extinguisher", "Assets/MarioParadiso/Built-In/Prefabs/Fireextinguisher.prefab",
            new Vector3(-9.25f, 0.25f, 18.4f), Quaternion.Euler(0f, 90f, 0f), Vector3.one * 0.85f,
            new Vector3(0.18f, 0.75f, 0.18f), red);

        AddPrefabOrFallback(root, "First_Aid_Kit", "Assets/MarioParadiso/Built-In/Prefabs/EHBO-Kit.prefab",
            new Vector3(-9.25f, 1.2f, 18.95f), Quaternion.Euler(0f, 90f, 0f), Vector3.one * 0.75f,
            new Vector3(0.42f, 0.32f, 0.12f), red);

        AddPrefabOrFallback(root, "Safety_Locker", "Assets/MarioParadiso/Built-In/Prefabs/Locker.prefab",
            new Vector3(-9.3f, 0f, 20.85f), Quaternion.Euler(0f, 90f, 0f), Vector3.one * 0.9f,
            new Vector3(0.7f, 1.8f, 0.45f), safetyGreen);
    }

    private void AddShelfLoad(Transform root, Vector3 rackCenter, Material yellow, Material metal)
    {
        AddPrefabOrFallback(root, "Rack_Box_Stack", "Assets/Material/CyberneticWalrus/Warehouse_Props_Vol1/Prefabs/Box_01a_Stack_Prefab_01.prefab",
            rackCenter + new Vector3(0.05f, 0.75f, -0.45f), Quaternion.Euler(0f, 90f, 0f), Vector3.one * 0.45f,
            new Vector3(0.55f, 0.35f, 0.45f), yellow);

        AddPrefabOrFallback(root, "Rack_Long_Box", "Assets/Material/CyberneticWalrus/Warehouse_Props_Vol1/Prefabs/Box_Long_01a_Stack_Prefab_01.prefab",
            rackCenter + new Vector3(0.05f, 1.25f, 0.4f), Quaternion.Euler(0f, 90f, 0f), Vector3.one * 0.42f,
            new Vector3(0.45f, 0.22f, 0.7f), yellow);

        AddPrefabOrFallback(root, "Rack_Parts_Bin", string.Empty,
            rackCenter + new Vector3(0.05f, 1.65f, -0.1f), Quaternion.identity, Vector3.one,
            new Vector3(0.46f, 0.24f, 0.52f), metal);
    }

    private GameObject AddPrefabOrFallback(
        Transform parent,
        string name,
        string assetPath,
        Vector3 localPosition,
        Quaternion localRotation,
        Vector3 localScale,
        Vector3 fallbackScale,
        Material fallbackMaterial)
    {
#if UNITY_EDITOR
        if (!string.IsNullOrWhiteSpace(assetPath))
        {
            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(assetPath);
            if (prefab != null)
            {
                var instance = (GameObject)PrefabUtility.InstantiatePrefab(prefab, parent);
                instance.name = "Generated_" + name;
                instance.transform.localPosition = localPosition;
                instance.transform.localRotation = localRotation;
                instance.transform.localScale = localScale;
                return instance;
            }
        }
#endif
        return AddBox(parent, name, localPosition, fallbackScale, fallbackMaterial, localRotation);
    }

    private GameObject AddBox(Transform parent, string name, Vector3 localPosition, Vector3 localScale, Material material, Quaternion? localRotation = null)
    {
        var box = GameObject.CreatePrimitive(PrimitiveType.Cube);
        box.name = "Generated_" + name;
        box.transform.SetParent(parent, false);
        box.transform.localPosition = localPosition;
        box.transform.localRotation = localRotation ?? Quaternion.identity;
        box.transform.localScale = localScale;

        var renderer = box.GetComponent<Renderer>();
        if (renderer != null && material != null)
        {
            renderer.sharedMaterial = material;
        }

        return box;
    }

    private Material CreateMaterial(string name, Color color)
    {
        var material = new Material(Shader.Find("Standard"))
        {
            name = "Generated_" + name,
            color = color
        };

        return material;
    }

    private void ClearGeneratedRoot()
    {
        var existing = transform.Find(GeneratedRootName);
        if (existing == null)
        {
            return;
        }

        if (Application.isPlaying)
        {
            Destroy(existing.gameObject);
        }
        else
        {
            DestroyImmediate(existing.gameObject);
        }
    }
}
