using System;
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

public static class RelinkExtractedGltfMaterials
{
    [MenuItem("Assets/GLTF/Relink Existing Extracted Materials", true)]
    private static bool ValidateRelink()
    {
        UnityEngine.Object obj = Selection.activeObject;
        if (obj == null)
            return false;

        string path = AssetDatabase.GetAssetPath(obj);

        return path.EndsWith(".glb", StringComparison.OrdinalIgnoreCase)
            || path.EndsWith(".gltf", StringComparison.OrdinalIgnoreCase);
    }

    [MenuItem("Assets/GLTF/Relink Existing Extracted Materials")]
    private static void Relink()
    {
        UnityEngine.Object selected = Selection.activeObject;
        string gltfPath = AssetDatabase.GetAssetPath(selected);

        if (string.IsNullOrEmpty(gltfPath))
        {
            Debug.LogError("无法获取 GLB/glTF 路径。");
            return;
        }

        string directory =
            Path.GetDirectoryName(gltfPath)?.Replace("\\", "/");

        string fileName =
            Path.GetFileNameWithoutExtension(gltfPath);

        string materialFolder =
            $"{directory}/{fileName}_Materials";

        if (!AssetDatabase.IsValidFolder(materialFolder))
        {
            Debug.LogError(
                $"找不到已提取材质目录：{materialFolder}"
            );
            return;
        }

        // --------------------------------------------------
        // 1. 读取 GLB 内的原始 Material
        // --------------------------------------------------

        UnityEngine.Object[] subAssets =
            AssetDatabase.LoadAllAssetsAtPath(gltfPath);

        Dictionary<string, Material> sourceByName =
            new Dictionary<string, Material>();

        foreach (UnityEngine.Object asset in subAssets)
        {
            if (asset is Material mat)
            {
                if (!sourceByName.ContainsKey(mat.name))
                    sourceByName.Add(mat.name, mat);
            }
        }

        // --------------------------------------------------
        // 2. 读取已经提取好的 .mat
        // --------------------------------------------------

        Dictionary<string, Material> extractedByName =
            new Dictionary<string, Material>();

        string[] guids =
            AssetDatabase.FindAssets(
                "t:Material",
                new[] { materialFolder });

        foreach (string guid in guids)
        {
            string path =
                AssetDatabase.GUIDToAssetPath(guid);

            Material mat =
                AssetDatabase.LoadAssetAtPath<Material>(path);

            if (mat == null)
                continue;

            if (!extractedByName.ContainsKey(mat.name))
            {
                extractedByName.Add(mat.name, mat);
            }
        }

        if (extractedByName.Count == 0)
        {
            Debug.LogError(
                $"目录中没有找到 .mat：{materialFolder}"
            );
            return;
        }

        // --------------------------------------------------
        // 3. 建立：
        // GLB 原始 Material -> 已提取 Material
        // --------------------------------------------------

        Dictionary<Material, Material> replacementMap =
            new Dictionary<Material, Material>();

        foreach (var pair in sourceByName)
        {
            if (extractedByName.TryGetValue(
                    pair.Key,
                    out Material extracted))
            {
                replacementMap.Add(
                    pair.Value,
                    extracted);
            }
            else
            {
                Debug.LogWarning(
                    $"未找到同名提取材质：{pair.Key}"
                );
            }
        }

        // --------------------------------------------------
        // 4. 替换当前所有打开场景里的引用
        // --------------------------------------------------

        int rendererCount = 0;
        int slotCount = 0;

        HashSet<Scene> modifiedScenes =
            new HashSet<Scene>();

        Renderer[] renderers =
            UnityEngine.Object.FindObjectsOfType<Renderer>(true);

        foreach (Renderer renderer in renderers)
        {
            Material[] materials =
                renderer.sharedMaterials;

            bool changed = false;

            for (int i = 0; i < materials.Length; i++)
            {
                Material current =
                    materials[i];

                if (current == null)
                    continue;

                if (!replacementMap.TryGetValue(
                        current,
                        out Material replacement))
                {
                    continue;
                }

                if (!changed)
                {
                    Undo.RecordObject(
                        renderer,
                        "Relink GLTF Materials");
                }

                materials[i] = replacement;
                slotCount++;
                changed = true;
            }

            if (!changed)
                continue;

            renderer.sharedMaterials = materials;

            EditorUtility.SetDirty(renderer);

            // 关键：保存 Prefab Instance Override
            if (PrefabUtility.IsPartOfPrefabInstance(renderer))
            {
                PrefabUtility
                    .RecordPrefabInstancePropertyModifications(
                        renderer);
            }

            rendererCount++;

            Scene scene =
                renderer.gameObject.scene;

            if (scene.IsValid())
                modifiedScenes.Add(scene);
        }

        // --------------------------------------------------
        // 5. 标记并保存场景
        // --------------------------------------------------

        foreach (Scene scene in modifiedScenes)
        {
            EditorSceneManager.MarkSceneDirty(scene);
        }

        bool saved =
            EditorSceneManager.SaveOpenScenes();

        Debug.Log(
            $"重新绑定完成。\n"
            + $"匹配材质：{replacementMap.Count}\n"
            + $"修改 Renderer：{rendererCount}\n"
            + $"替换材质槽：{slotCount}\n"
            + $"保存 Scene：{saved}"
        );
    } 
} 