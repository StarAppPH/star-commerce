
class Server {
    public apiUrl: string | null;
    constructor(environment:string) {
        if (!['cloudprod','localprod','localdev','clouddev'].includes(environment)) {
            throw new Error('Invalid environment');
        }
        const urls = { localApi: 'http://127.0.0.1:5003', productionApi: process.env.NEXT_PUBLIC_API_URL || '' };
        this.apiUrl = ['cloudprod','localprod'].includes(environment) ? urls.productionApi : urls.localApi;
    }
}
export default Server;