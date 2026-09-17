using System.Collections.Generic;
using UnityEditor;
using UnityEngine;

public static class WallBoundsBaker
{
    private const string RootName = "WALL_BOUNDS_EXPORT";

    [MenuItem("Tools/Wall Tools/Bake Selected Wall Boxes")]
    private static void BakeSelectedWallBoxes()
    {
        GameObject[] selectedObjects = Selection.gameObjects;

        if (selectedObjects == null || selectedObjects.Length == 0)
        {
            Debug.LogWarning("请先在 Hierarchy 中选择墙体或墙体根节点。");
            return;
        }

        // 避免父子对象同时被选择后重复处理同一个 MeshFilter
        HashSet<MeshFilter> meshFilters = new HashSet<MeshFilter>();

        foreach (GameObject selected in selectedObjects)
        {
            MeshFilter[] children =
                selected.GetComponentsInChildren<MeshFilter>(true);

            foreach (MeshFilter mf in children)
            {
                if (mf.sharedMesh != null)
                    meshFilters.Add(mf);
            }
        }

        if (meshFilters.Count == 0)
        {
            Debug.LogWarning("选中范围内没有找到有效的 MeshFilter。");
            return;
        }

        GameObject exportRoot = new GameObject(RootName);

        exportRoot.transform.position = Vector3.zero;
        exportRoot.transform.rotation = Quaternion.identity;
        exportRoot.transform.localScale = Vector3.one;

        Undo.RegisterCreatedObjectUndo(
            exportRoot,
            "Bake Wall Boxes"
        );

        int index = 0;

        foreach (MeshFilter mf in meshFilters)
        {
            Mesh sourceMesh = mf.sharedMesh;

            Bounds b = sourceMesh.bounds;

            if (b.size.x <= 0f ||
                b.size.y <= 0f ||
                b.size.z <= 0f)
            {
                Debug.LogWarning(
                    $"跳过零尺寸 Mesh: {mf.name}",
                    mf
                );

                continue;
            }

            Matrix4x4 localToWorld =
                mf.transform.localToWorldMatrix;

            Vector3 min = b.min;
            Vector3 max = b.max;

            // Mesh local bounds 的 8 个角
            Vector3[] localCorners =
            {
                new Vector3(min.x, min.y, min.z), // 0
                new Vector3(max.x, min.y, min.z), // 1
                new Vector3(max.x, min.y, max.z), // 2
                new Vector3(min.x, min.y, max.z), // 3

                new Vector3(min.x, max.y, min.z), // 4
                new Vector3(max.x, max.y, min.z), // 5
                new Vector3(max.x, max.y, max.z), // 6
                new Vector3(min.x, max.y, max.z)  // 7
            };

            // 直接烘焙为世界空间顶点
            Vector3[] worldCorners = new Vector3[8];

            for (int i = 0; i < 8; i++)
            {
                worldCorners[i] =
                    localToWorld.MultiplyPoint3x4(
                        localCorners[i]
                    );
            }

            Mesh boxMesh = CreateBoxMesh(worldCorners);

            boxMesh.name =
                $"WallBoxMesh_{index:D4}_{mf.gameObject.name}";

            GameObject boxObject =
                new GameObject(
                    $"WallBox_{index:D4}_{mf.gameObject.name}"
                );

            Undo.RegisterCreatedObjectUndo(
                boxObject,
                "Create Wall Box"
            );

            boxObject.transform.SetParent(
                exportRoot.transform,
                false
            );

            // 顶点已经是世界空间坐标，
            // 所以对象本身保持 Identity Transform
            boxObject.transform.localPosition =
                Vector3.zero;

            boxObject.transform.localRotation =
                Quaternion.identity;

            boxObject.transform.localScale =
                Vector3.one;

            MeshFilter newMF =
                boxObject.AddComponent<MeshFilter>();

            newMF.sharedMesh = boxMesh;

            MeshRenderer newMR =
                boxObject.AddComponent<MeshRenderer>();

            // 只为了方便 Scene 中看见
            MeshRenderer oldMR =
                mf.GetComponent<MeshRenderer>();

            if (oldMR != null)
            {
                newMR.sharedMaterials =
                    oldMR.sharedMaterials;
            }

            index++;
        }

        Selection.activeGameObject = exportRoot;

        Debug.Log(
            $"Wall Bounds Baker 完成：生成 {index} 个世界空间 Wall Box。"
        );
    }

    private static Mesh CreateBoxMesh(
        Vector3[] v
    )
    {
        Mesh mesh = new Mesh();

        mesh.vertices = v;

        /*
         * 顶点：
         *
         *       7------6
         *      /|     /|
         *     4------5 |
         *     | 3----|-2
         *     |/     |/
         *     0------1
         */

        int[] triangles =
        {
            // Bottom
            0, 2, 1,
            0, 3, 2,

            // Top
            4, 5, 6,
            4, 6, 7,

            // Front
            0, 1, 5,
            0, 5, 4,

            // Right
            1, 2, 6,
            1, 6, 5,

            // Back
            2, 3, 7,
            2, 7, 6,

            // Left
            3, 0, 4,
            3, 4, 7
        };

        mesh.triangles = triangles;

        mesh.RecalculateNormals();
        mesh.RecalculateBounds();

        return mesh;
    }

    [MenuItem(
        "Tools/Wall Tools/Bake Selected Wall Boxes",
        true
    )]
    private static bool ValidateBake()
    {
        return Selection.gameObjects != null &&
               Selection.gameObjects.Length > 0;
    }
}