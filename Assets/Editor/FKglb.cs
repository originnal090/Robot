using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEngine;
using UnityEngine.SceneManagement;

public static class ExtractGltfMaterials
{
    [MenuItem("Assets/GLTF/Extract Materials And Replace Scene References", true)]
    private static bool ValidateExtractMaterials()
    {
        Object obj = Selection.activeObject;
        if (obj == null)
            return false;

        string path = AssetDatabase.GetAssetPath(obj);

        return path.EndsWith(".glb", System.StringComparison.OrdinalIgnoreCase)
            || path.EndsWith(".gltf", System.StringComparison.OrdinalIgnoreCase);
    }

    [MenuItem("Assets/GLTF/Extract Materials And Replace Scene References")]
    private static void ExtractMaterials()
    {
        Object selected = Selection.activeObject;
        string gltfPath = AssetDatabase.GetAssetPath(selected);

        if (string.IsNullOrEmpty(gltfPath))
        {
            Debug.LogError("无法获取选中的 glTF/GLB 路径。");
            return;
        }

        // 找到 GLB / glTF 内部所有子资源
        Object[] subAssets = AssetDatabase.LoadAllAssetsAtPath(gltfPath);

        List<Material> sourceMaterials = new List<Material>();

        foreach (Object asset in subAssets)
        {
            if (asset is Material material)
            {
                sourceMaterials.Add(material);
            }
        }

        if (sourceMaterials.Count == 0)
        {
            Debug.LogWarning($"没有在 {gltfPath} 中找到 Material 子资源。");
            return;
        }

        // 创建输出目录：
        // xxx.glb
        // xxx_Materials/
        string gltfDirectory = Path.GetDirectoryName(gltfPath);
        string gltfFileName = Path.GetFileNameWithoutExtension(gltfPath);

        string materialFolder =
            $"{gltfDirectory}/{gltfFileName}_Materials";

        if (!AssetDatabase.IsValidFolder(materialFolder))
        {
            AssetDatabase.CreateFolder(
                gltfDirectory,
                $"{gltfFileName}_Materials"
            );
        }

        // 建立：
        // 原始只读 Material -> 新独立 Material
        Dictionary<Material, Material> materialMap =
            new Dictionary<Material, Material>();

        foreach (Material sourceMaterial in sourceMaterials)
        {
            Material copiedMaterial =
                new Material(sourceMaterial);

            copiedMaterial.name = sourceMaterial.name;

            string safeName =
                MakeSafeFileName(sourceMaterial.name);

            string targetPath =
                $"{materialFolder}/{safeName}.mat";

            // 不覆盖已有文件，自动变成 xxx 1.mat 等
            targetPath =
                AssetDatabase.GenerateUniqueAssetPath(targetPath);

            AssetDatabase.CreateAsset(
                copiedMaterial,
                targetPath
            );

            materialMap.Add(
                sourceMaterial,
                copiedMaterial
            );

            Debug.Log(
                $"提取材质：{sourceMaterial.name} -> {targetPath}"
            );
        }

        AssetDatabase.SaveAssets();
        AssetDatabase.Refresh();

        int replacedRendererCount = 0;
        int replacedMaterialSlotCount = 0;

        // 当前打开场景中的 MeshRenderer
        MeshRenderer[] meshRenderers =
            Object.FindObjectsOfType<MeshRenderer>(true);

        foreach (MeshRenderer renderer in meshRenderers)
        {
            if (ReplaceRendererMaterials(
                    renderer,
                    materialMap,
                    ref replacedMaterialSlotCount))
            {
                replacedRendererCount++;
            }
        }

        // 当前打开场景中的 SkinnedMeshRenderer
        SkinnedMeshRenderer[] skinnedRenderers =
            Object.FindObjectsOfType<SkinnedMeshRenderer>(true);

        foreach (SkinnedMeshRenderer renderer in skinnedRenderers)
        {
            if (ReplaceRendererMaterials(
                    renderer,
                    materialMap,
                    ref replacedMaterialSlotCount))
            {
                replacedRendererCount++;
            }
        }

        // 标记场景已修改
        Scene activeScene = SceneManager.GetActiveScene();

        if (activeScene.IsValid())
        {
            UnityEditor.SceneManagement.EditorSceneManager
                .MarkSceneDirty(activeScene);
        }

        Debug.Log(
            $"完成。\n" +
            $"提取 Material：{materialMap.Count}\n" +
            $"修改 Renderer：{replacedRendererCount}\n" +
            $"替换材质槽：{replacedMaterialSlotCount}\n" +
            $"输出目录：{materialFolder}"
        );

        // 在 Project 中选中输出文件夹
        Object folderAsset =
            AssetDatabase.LoadAssetAtPath<Object>(materialFolder);

        Selection.activeObject = folderAsset;
        EditorGUIUtility.PingObject(folderAsset);
    }

    private static bool ReplaceRendererMaterials(
        Renderer renderer,
        Dictionary<Material, Material> materialMap,
        ref int replacedSlotCount)
    {
        Material[] materials =
            renderer.sharedMaterials;

        bool changed = false;

        for (int i = 0; i < materials.Length; i++)
        {
            Material currentMaterial =
                materials[i];

            if (currentMaterial == null)
                continue;

            if (materialMap.TryGetValue(
                    currentMaterial,
                    out Material replacement))
            {
                if (!changed)
                {
                    Undo.RecordObject(
                        renderer,
                        "Replace GLTF Materials"
                    );
                }

                materials[i] = replacement;

                replacedSlotCount++;
                changed = true;
            }
        }

        if (changed)
        {
            renderer.sharedMaterials = materials;

            EditorUtility.SetDirty(renderer);
        }

        return changed;
    }

    private static string MakeSafeFileName(string name)
    {
        foreach (char invalidChar
                 in Path.GetInvalidFileNameChars())
        {
            name = name.Replace(
                invalidChar,
                '_'
            );
        }

        return name;
    }
} 