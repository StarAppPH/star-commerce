import Server from "./Server";
import type { ApiResponse } from "./ApiResponse";
import type { DevUserListResponse } from '../schemas/DevUserListResponseSchema';
import type { FirebaseUserObject } from '../schemas/FirebaseUserObjectSchema';
import type { HandshakeResponse } from '../schemas/HandshakeResponseSchema';
import type { NoContentResponse } from '../schemas/NoContentResponseSchema';
import type { RoleListResponse } from '../schemas/RoleListResponseSchema';
import type { SampleTodoListResponse } from '../schemas/SampleTodoListResponseSchema';
import type { UserResponse } from '../schemas/UserResponseSchema';

    class ServerRequests extends Server {
    constructor(environment : string) {
        super(environment);
    }


    async loginWithGoogle(firebaseUserObject: FirebaseUserObject): Promise<ApiResponse<UserResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/loginWithGoogle`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({"firebaseUserObject": firebaseUserObject}),
            cache: 'no-store'
        });
        const data: ApiResponse<UserResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        if (data.access_token) { localStorage.setItem("access_token", data.access_token); }
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async devGetUserList(): Promise<ApiResponse<DevUserListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/devGetUserList`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({}),
            cache: 'no-store'
        });
        const data: ApiResponse<DevUserListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async devLogin(userId: string): Promise<ApiResponse<UserResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/devLogin`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<UserResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        if (data.access_token) { localStorage.setItem("access_token", data.access_token); }
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getRoles(userId: string): Promise<ApiResponse<RoleListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getRoles`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<RoleListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getUserRoleTypes(userId: string): Promise<ApiResponse<NoContentResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getUserRoleTypes`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<NoContentResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async handshake(): Promise<ApiResponse<HandshakeResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/handshake`, {
            method: 'GET',
            headers: {
            "Content-Type": "application/json",
            
            },
            
            cache: 'no-store'
        });
        const data: ApiResponse<HandshakeResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getTodos(): Promise<ApiResponse<SampleTodoListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getTodos`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({}),
            cache: 'no-store'
        });
        const data: ApiResponse<SampleTodoListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async createTodo(title: string): Promise<ApiResponse<NoContentResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/createTodo`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({"title": title}),
            cache: 'no-store'
        });
        const data: ApiResponse<NoContentResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async markTodoAsCompleted(todoId: string): Promise<ApiResponse<NoContentResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/markTodoAsCompleted`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({"todoId": todoId}),
            cache: 'no-store'
        });
        const data: ApiResponse<NoContentResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async markTodoAsUnfinished(todoId: string): Promise<ApiResponse<NoContentResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/markTodoAsUnfinished`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({"todoId": todoId}),
            cache: 'no-store'
        });
        const data: ApiResponse<NoContentResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async deleteTodo(todoId: string): Promise<ApiResponse<NoContentResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/deleteTodo`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({"todoId": todoId}),
            cache: 'no-store'
        });
        const data: ApiResponse<NoContentResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }
}

export interface RouteResponseModels {
    loginWithGoogle: UserResponse;
    devGetUserList: DevUserListResponse;
    devLogin: UserResponse;
    getRoles: RoleListResponse;
    getUserRoleTypes: NoContentResponse;
    handshake: HandshakeResponse;
    getTodos: SampleTodoListResponse;
    createTodo: NoContentResponse;
    markTodoAsCompleted: NoContentResponse;
    markTodoAsUnfinished: NoContentResponse;
    deleteTodo: NoContentResponse;
}

export interface RouteRequestBodies {
    loginWithGoogle: { firebaseUserObject: FirebaseUserObject };
    devGetUserList: Record<string, never>;
    devLogin: { userId: string };
    getRoles: { userId: string };
    getUserRoleTypes: { userId: string };
    handshake: Record<string, never>;
    getTodos: Record<string, never>;
    createTodo: { title: string };
    markTodoAsCompleted: { todoId: string };
    markTodoAsUnfinished: { todoId: string };
    deleteTodo: { todoId: string };
}

export type PublicRoute =
    | 'devGetUserList'
    | 'getTodos'
    | 'createTodo'
    | 'markTodoAsCompleted'
    | 'markTodoAsUnfinished'
    | 'deleteTodo';

export default ServerRequests;
