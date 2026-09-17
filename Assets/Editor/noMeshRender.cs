using UnityEditor;
using UnityEngine;
 
public static class MeshRendererTools
{
    [MenuItem("Tools/Mesh Renderer/Disable In Selected Hierarchy")]
    private static void DisableMeshRenderers()
    {
        GameObject root = Selection.activeGameObject;

        if (root == null)
        {
            Debug.LogWarning("Please select a GameObject in the Hierarchy.");
            return;
        }

        MeshRenderer[] renderers =
            root.GetComponentsInChildren<MeshRenderer>(true);

        Undo.RecordObjects(renderers, "Disable Mesh Renderers");

        foreach (MeshRenderer renderer in renderers)
        {
            renderer.enabled = false;
            EditorUtility.SetDirty(renderer);
        }

        Debug.Log(
            $"Disabled {renderers.Length} MeshRenderers under '{root.name}'."
        );
    }

    [MenuItem("Tools/Mesh Renderer/Enable In Selected Hierarchy")]
    private static void EnableMeshRenderers()
    {
        GameObject root = Selection.activeGameObject;

        if (root == null)
        {
            Debug.LogWarning("Please select a GameObject in the Hierarchy.");
            return;
        }

        MeshRenderer[] renderers =
            root.GetComponentsInChildren<MeshRenderer>(true);

        Undo.RecordObjects(renderers, "Enable Mesh Renderers");

        foreach (MeshRenderer renderer in renderers)
        {
            renderer.enabled = true;
            EditorUtility.SetDirty(renderer);
        }

        Debug.Log(
            $"Enabled {renderers.Length} MeshRenderers under '{root.name}'."
        );
    }
}